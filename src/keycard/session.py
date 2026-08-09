"""The bridge between an SSH channel and a room's pty.

This is implemented as an ``SSHServerSession`` rather than via asyncssh's
higher-level process API because we need ``terminal_size_changed``. Window
resize is the thing that makes a session feel real, and the process API does
not surface it.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Iterator
from typing import Any

import asyncssh

from . import banner
from .backends.base import Backend, Kept, Room
from .config import RoomConfig

log = logging.getLogger(__name__)

# Exit status GNU coreutils' `timeout` uses when it kills a command — reusing
# it here means an idle-reaped session is at least recognisable in scripts.
IDLE_EXIT_STATUS = 124

# What a POSIX shell reports for a process killed by SIGTERM (128 + 15) — a
# session cut short by a server shutdown is, in effect, exactly that.
SHUTDOWN_EXIT_STATUS = 143

# How many chunks of type-ahead to hold while the room is still opening.
# There is a real window here — a container takes about a second to start and
# a microVM several, and anything typed in it arrives before there is a pty
# to type it into. Bounded because the far end is a network peer: past this
# many unread chunks the session is not type-ahead any more, it is a flood.
INPUT_BACKLOG = 512


class ActiveSessions:
    """Live sessions a shutdown drain can wait on without polling a set."""

    def __init__(self) -> None:
        self._sessions: set[RoomSession] = set()
        self._empty = asyncio.Event()
        self._empty.set()

    def add(self, session: RoomSession) -> None:
        self._sessions.add(session)
        self._empty.clear()

    def discard(self, session: RoomSession) -> None:
        self._sessions.discard(session)
        if not self._sessions:
            self._empty.set()

    def __len__(self) -> int:
        return len(self._sessions)

    def __iter__(self) -> Iterator[RoomSession]:
        return iter(self._sessions)

    def __contains__(self, session: object) -> bool:
        return session in self._sessions

    async def wait_empty(self) -> None:
        """Block until every session has released. Callers bound this with
        ``asyncio.timeout`` — it never returns early on its own."""
        await self._empty.wait()


class KeptRooms:
    """Rooms paused on disconnect, held for reconnect within a keep window.

    Keyed by username: reconnecting under that username reclaims the paused
    room. This doesn't add a new trust boundary — every authorised key is
    already treated as equally trusted (see SECURITY.md), so any keyholder
    who could `ssh python@host` before could already reach this room anyway.
    """

    def __init__(self, backend: Backend, window_seconds: float) -> None:
        self._backend = backend
        self.window_seconds = window_seconds
        self._by_username: dict[str, Kept] = {}

    @property
    def enabled(self) -> bool:
        return self.window_seconds > 0

    def take(self, username: str) -> Kept | None:
        """Claim a paused room for reconnect, cancelling its expiry."""
        return self._by_username.pop(username, None)

    async def keep(self, username: str, room: Room) -> None:
        """Pause *room* and hold it for `window_seconds`, destroying it if
        nothing calls `take()` first."""
        kept = await room.pause()
        self._by_username[username] = kept
        asyncio.create_task(self._expire(username, kept))  # noqa: RUF006

    async def _expire(self, username: str, kept: Kept) -> None:
        await asyncio.sleep(self.window_seconds)
        if self._by_username.get(username) is kept:
            del self._by_username[username]
            await self._backend.destroy_kept(kept)
            log.info("kept room for %r expired; destroyed", username)

    async def destroy_all(self) -> None:
        """Sweep every paused room. Called on server shutdown — once the
        process exits, nothing is left to run the expiry timers above, so
        anything still kept would otherwise leak indefinitely."""
        for username, kept in list(self._by_username.items()):
            if self._by_username.get(username) is kept:
                del self._by_username[username]
                await self._backend.destroy_kept(kept)


class RoomSession(asyncssh.SSHServerSession[bytes]):
    def __init__(
        self,
        backend: Backend,
        room: RoomConfig,
        idle_seconds: float = 0.0,
        active: ActiveSessions | None = None,
        username: str = "",
        kept: KeptRooms | None = None,
    ) -> None:
        self._backend = backend
        self._room_cfg = room
        self._idle_seconds = idle_seconds
        # Registry the server drains on shutdown. None in tests that don't care.
        self._active = active
        self._username = username
        # Registry for --keep reconnects. None means the feature is off.
        self._kept = kept
        self._chan: asyncssh.SSHServerChannel[bytes] | None = None
        self._room: Room | None = None
        self._pump: asyncio.Task[None] | None = None
        self._feeder: asyncio.Task[None] | None = None
        self._watchdog: asyncio.Task[None] | None = None
        # The one teardown this session will ever run. Held as a task rather
        # than run inline so that cancelling `_pump` cannot abort a destroy
        # that is already under way — see `_begin_teardown`.
        self._teardown: asyncio.Task[None] | None = None
        self._status: int | None = None
        # Input is queued rather than written straight through: until `open()`
        # returns there is no room to write to, and the first thing a client
        # sends usually arrives during exactly that window.
        self._input: asyncio.Queue[bytes] = asyncio.Queue(INPUT_BACKLOG)
        self._size = (80, 24)
        self._last_activity = time.monotonic()

    # -- channel lifecycle -------------------------------------------------

    def connection_made(self, chan: asyncssh.SSHServerChannel[bytes]) -> None:
        self._chan = chan
        if self._active is not None:
            self._active.add(self)

    def pty_requested(
        self, term_type: str, term_size: tuple[int, int, int, int], term_modes: Any
    ) -> bool:
        self._size = (term_size[0] or 80, term_size[1] or 24)
        return True

    def shell_requested(self) -> bool:
        return True

    def exec_requested(self, command: str) -> bool:
        # v1 is interactive only. Accepting exec would mean a second, subtly
        # different code path; better to refuse clearly than half-support it.
        return False

    def session_started(self) -> None:
        self._pump = asyncio.create_task(self._run())
        self._watchdog = asyncio.create_task(self._idle_watchdog())

    def terminal_size_changed(self, width: int, height: int, pixwidth: int, pixheight: int) -> None:
        # Recorded as well as forwarded: a resize that lands while the room is
        # still opening has nothing to forward to, and `open()` is about to be
        # told what size to use.
        self._size = (width or self._size[0], height or self._size[1])
        if self._room is not None:
            asyncio.create_task(self._room.resize(width, height))  # noqa: RUF006

    def data_received(self, data: bytes, datatype: int | None) -> None:
        self._last_activity = time.monotonic()
        try:
            self._input.put_nowait(data)
        except asyncio.QueueFull:
            # Nothing has drained this in `INPUT_BACKLOG` chunks, so the room
            # is wedged or the peer is flooding. Dropping the newest keeps
            # what was typed first, which is the half more likely to matter.
            log.warning("input backlog full; dropped %d bytes", len(data))

    def eof_received(self) -> bool:
        return False

    def connection_lost(self, exc: Exception | None) -> None:
        # This fires whether the user typed exit or the network died, which
        # is exactly the guarantee keycard is selling — the room was still
        # live, so either destroy it or (with --keep) pause it for reconnect.
        if self._pump is not None:
            self._pump.cancel()
        self._stop_feeder()
        if self._watchdog is not None:
            self._watchdog.cancel()
        self._begin_teardown(allow_keep=True)

    def _begin_teardown(self, *, allow_keep: bool) -> asyncio.Task[None]:
        """Start this session's teardown, or hand back the one already going.

        Four paths can each decide the room should end — the pump finishing,
        the client hanging up, the idle reaper, and a shutdown drain — and any
        two of them can arrive together. Funnelling all four through one task
        means the room is torn down exactly once, and, because every caller
        gets that same task back to await, nobody walks away believing
        teardown is finished while it is still running.

        That second half is what shutdown depends on. `_release` is what tells
        the drain a session is done, so releasing early lets `serve()` reach
        `backend.close()` and process exit with a destroy still in flight —
        which `asyncio.run` then cancels, leaving the room behind.
        """
        if self._teardown is None:
            self._teardown = asyncio.create_task(self._run_teardown(allow_keep=allow_keep))
        return self._teardown

    async def _run_teardown(self, *, allow_keep: bool) -> None:
        room, self._room = self._room, None
        try:
            if room is None:
                return
            if allow_keep and self._kept is not None and self._kept.enabled:
                try:
                    await self._kept.keep(self._username, room)
                    log.info(
                        "room paused for %r (%.0fs to reconnect)",
                        self._username,
                        self._kept.window_seconds,
                    )
                    return
                except Exception:
                    log.exception("pause failed; destroying room instead")
            self._status = await self._checkout(room)
        finally:
            self._release()

    def _release(self) -> None:
        # A drain loop watches this set shrink to know teardown is done —
        # release only after the room is actually gone, not merely detached.
        if self._active is not None:
            self._active.discard(self)

    # -- the pump ----------------------------------------------------------

    async def _run(self) -> None:
        chan = self._chan
        if chan is None:  # pragma: no cover - connection_made always runs first
            return
        kept = self._kept.take(self._username) if self._kept is not None else None
        try:
            if kept is not None:
                self._room = await self._backend.resume(kept, *self._size)
            else:
                self._room = await self._backend.open(self._room_cfg, *self._size)
        except Exception:
            log.exception("could not open room")
            if kept is not None:
                # take() already claimed it — nothing else will ever clean
                # this container up if we don't.
                try:
                    await self._backend.destroy_kept(kept)
                except Exception:
                    log.exception("cleanup of failed resume also failed")
            chan.write(b"keycard: no room available\r\n")
            chan.exit(1)
            return

        if self._teardown is not None:
            # The session ended while the room was still opening — a shutdown
            # during a microVM boot is long enough for this to be ordinary.
            # Teardown has already run and found no room to take, so this one
            # would never be collected by anything else.
            log.info("room opened after the session ended; destroying it")
            room, self._room = self._room, None
            await self._checkout(room)
            return

        if kept is not None:
            chan.write(banner.resumed(self._room_cfg.name, self._room_cfg.image))
        else:
            chan.write(banner.accepted(self._room_cfg.name, self._room_cfg.image))
        room = self._room
        # Only now is there something to write to, so this is where the
        # type-ahead collected during `open()` finally goes.
        self._feeder = asyncio.create_task(self._feed(room))
        try:
            while True:
                data = await room.read()
                if not data:
                    break
                self._last_activity = time.monotonic()
                chan.write(data)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("room stream failed")
        finally:
            self._stop_feeder()
            # Whether the shell exiting is what ended this session, or whether
            # something else got there first. Only the former has a checkout to
            # announce: if the client already hung up, or a shutdown drain is
            # tearing this down, the goodbye is both wrong and unreadable.
            ours = self._teardown is None
            # Awaited, not started and forgotten: the goodbye below reports
            # the shell's exit status, which is not known until the room has
            # actually been checked out.
            await self._begin_teardown(allow_keep=False)
            if ours and self._status is not None:
                # `is_closing()` is not a guarantee the write will land: a
                # channel can be past sending without yet reporting closing,
                # which is exactly what a server shutdown racing this produces.
                # The room is already gone by here, so a goodbye nobody can
                # receive is not worth an unretrieved exception in the log.
                try:
                    if not chan.is_closing():
                        chan.write(banner.destroyed("checked out"))
                        chan.exit(self._status)
                except (BrokenPipeError, ConnectionResetError, OSError):
                    log.debug("channel gone before checkout could be reported")

    async def _feed(self, room: Room) -> None:
        """Queued client input into the room, in order.

        A single task rather than a write per `data_received` callback: two
        chunks written as two independent tasks can reach the pty in the
        wrong order, and a keystroke that arrives out of order is worse than
        one that arrives late.
        """
        while True:
            data = await self._input.get()
            await room.write(data)

    def _stop_feeder(self) -> None:
        if self._feeder is not None:
            self._feeder.cancel()
            self._feeder = None

    async def _checkout(self, room: Room) -> int:
        try:
            status = await room.destroy()
            log.info("room destroyed (status %s)", status)
            return status
        except Exception:
            log.exception("checkout failed")
            return 1

    # -- server shutdown -----------------------------------------------------

    def notify_shutdown(self, message: bytes) -> None:
        """Warn a connected client before the grace period runs out."""
        chan = self._chan
        if chan is not None and not chan.is_closing():
            chan.write(message)

    async def force_close(self) -> None:
        """Cut the session short once the shutdown grace period has expired.

        Drives teardown here rather than leaving it to `connection_lost` — the
        caller is a drain that has to know the room is really gone before the
        process exits, so it needs something to await. A client that hung up
        moments earlier already has a teardown running; this awaits that one
        instead of starting a second.
        """
        chan = self._chan
        if chan is not None and not chan.is_closing():
            chan.write(banner.destroyed("server shutting down"))

        await self._begin_teardown(allow_keep=False)

        if chan is not None and not chan.is_closing():
            chan.exit(SHUTDOWN_EXIT_STATUS)
            chan.close()

    # -- idle reaper ---------------------------------------------------------

    async def _idle_watchdog(self) -> None:
        """Reap the room after `idle_seconds` with no traffic either way.

        A clean exit or a dropped TCP connection are both already handled by
        `connection_lost`. This is the net under that: a connection that
        never sends a FIN — a dead wifi link, a suspended laptop — leaves the
        socket open and the room running forever without it.
        """
        if self._idle_seconds <= 0:
            return

        while True:
            remaining = self._idle_seconds - (time.monotonic() - self._last_activity)
            if remaining <= 0:
                break
            await asyncio.sleep(remaining)

        log.info("room idle for %.0fs; reaping", self._idle_seconds)
        chan = self._chan
        if chan is not None and not chan.is_closing():
            chan.write(banner.destroyed("idle timeout"))

        await self._begin_teardown(allow_keep=False)

        if chan is not None and not chan.is_closing():
            chan.exit(IDLE_EXIT_STATUS)
            chan.close()
