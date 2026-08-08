"""Unit tests for RoomSession's pump and idle-timeout reaper (no daemon needed)."""

from __future__ import annotations

import asyncio

import pytest

from keycard.backends.base import Backend, Kept, Room
from keycard.config import RoomConfig
from keycard.session import (
    IDLE_EXIT_STATUS,
    SHUTDOWN_EXIT_STATUS,
    ActiveSessions,
    KeptRooms,
    RoomSession,
)


class FakeKept(Kept):
    def __init__(self, room: FakeRoom) -> None:
        self.room = room


class FakeRoom(Room):
    def __init__(self) -> None:
        self.destroy_count = 0
        self.pause_count = 0
        self.destroy_kept_count = 0
        self._queue: asyncio.Queue[bytes] = asyncio.Queue()
        self.writes: list[bytes] = []

    async def feed(self, data: bytes) -> None:
        await self._queue.put(data)

    async def read(self) -> bytes:
        return await self._queue.get()

    async def write(self, data: bytes) -> None:
        self.writes.append(data)

    async def resize(self, width: int, height: int) -> None:
        pass

    async def destroy(self) -> int:
        self.destroy_count += 1
        await self._queue.put(b"")  # unblock a pending read()
        return 0

    async def pause(self) -> FakeKept:
        self.pause_count += 1
        return FakeKept(self)


class FakeBackend(Backend):
    def __init__(self, room: FakeRoom) -> None:
        self._room = room

    async def open(self, room: RoomConfig, width: int, height: int) -> Room:
        return self._room

    async def resume(self, kept: Kept, width: int, height: int) -> Room:
        assert isinstance(kept, FakeKept)
        return kept.room

    async def destroy_kept(self, kept: Kept) -> None:
        assert isinstance(kept, FakeKept)
        kept.room.destroy_kept_count += 1

    async def close(self) -> None:
        pass


class FakeChannel:
    def __init__(self) -> None:
        self.written: list[bytes] = []
        self.exit_status: int | None = None
        self._closing = False

    def write(self, data: bytes) -> None:
        self.written.append(data)

    def exit(self, status: int) -> None:
        self.exit_status = status

    def is_closing(self) -> bool:
        return self._closing

    def close(self) -> None:
        self._closing = True


class SlowBackend(FakeBackend):
    """A backend whose `open()` takes long enough for a client to type into.

    Not an artificial delay: a Firecracker room really is a kernel boot away,
    and that is the window this exists to reproduce.
    """

    def __init__(self, room: FakeRoom, delay: float) -> None:
        super().__init__(room)
        self._delay = delay
        self.opened_size: tuple[int, int] | None = None

    async def open(self, room: RoomConfig, width: int, height: int) -> Room:
        self.opened_size = (width, height)
        await asyncio.sleep(self._delay)
        return await super().open(room, width, height)


async def _teardown(session: RoomSession) -> None:
    """Cancel background tasks so a test doesn't leak them past its scope."""
    tasks = [t for t in (session._pump, session._feeder, session._watchdog) if t is not None]
    for t in tasks:
        t.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


@pytest.mark.timeout(5)
async def test_idle_timeout_reaps_a_silent_room() -> None:
    room = FakeRoom()
    chan = FakeChannel()
    session = RoomSession(
        FakeBackend(room), RoomConfig(name="ubuntu", image="ubuntu:24.04"), idle_seconds=0.05
    )
    session.connection_made(chan)  # type: ignore[arg-type]
    session.session_started()

    await asyncio.sleep(0.3)

    assert room.destroy_count == 1
    assert chan.exit_status == IDLE_EXIT_STATUS
    assert chan.is_closing()

    await _teardown(session)


@pytest.mark.timeout(5)
async def test_activity_postpones_the_reap() -> None:
    room = FakeRoom()
    chan = FakeChannel()
    session = RoomSession(
        FakeBackend(room), RoomConfig(name="ubuntu", image="ubuntu:24.04"), idle_seconds=0.15
    )
    session.connection_made(chan)  # type: ignore[arg-type]
    session.session_started()

    # Keep feeding input for longer than idle_seconds; the room must survive.
    for _ in range(5):
        await asyncio.sleep(0.08)
        session.data_received(b"x", None)
    assert room.destroy_count == 0

    # Now go quiet and let the timer catch up.
    await asyncio.sleep(0.3)
    assert room.destroy_count == 1

    await _teardown(session)


@pytest.mark.timeout(5)
async def test_zero_idle_seconds_disables_the_reaper() -> None:
    room = FakeRoom()
    chan = FakeChannel()
    session = RoomSession(
        FakeBackend(room), RoomConfig(name="ubuntu", image="ubuntu:24.04"), idle_seconds=0.0
    )
    session.connection_made(chan)  # type: ignore[arg-type]
    session.session_started()

    await asyncio.sleep(0.2)

    assert room.destroy_count == 0
    assert chan.exit_status is None

    await _teardown(session)


@pytest.mark.timeout(5)
async def test_room_output_also_counts_as_activity() -> None:
    room = FakeRoom()
    chan = FakeChannel()
    session = RoomSession(
        FakeBackend(room), RoomConfig(name="ubuntu", image="ubuntu:24.04"), idle_seconds=0.15
    )
    session.connection_made(chan)  # type: ignore[arg-type]
    session.session_started()

    for _ in range(5):
        await asyncio.sleep(0.08)
        await room.feed(b"still building...\n")
    assert room.destroy_count == 0

    await asyncio.sleep(0.3)
    assert room.destroy_count == 1

    await _teardown(session)


@pytest.mark.timeout(5)
async def test_input_typed_while_the_room_opens_is_not_lost() -> None:
    """Type-ahead has to survive `open()`.

    The first thing a client sends arrives while the room is still being
    created, and writing it straight to `self._room` dropped it on the floor
    because there was no room yet. A Docker room opens fast enough to hide
    that; a microVM boot does not, and the command the user typed simply
    never ran.
    """
    room = FakeRoom()
    chan = FakeChannel()
    backend = SlowBackend(room, delay=0.2)
    session = RoomSession(backend, RoomConfig(name="ubuntu", image="ubuntu:24.04"))
    session.connection_made(chan)  # type: ignore[arg-type]
    session.session_started()

    # Typed before open() has returned — there is no room to write to yet.
    session.data_received(b"echo one\n", None)
    session.data_received(b"echo two\n", None)
    assert room.writes == []

    await asyncio.sleep(0.4)

    # Delivered once there was somewhere to deliver them, and in order.
    assert room.writes == [b"echo one\n", b"echo two\n"]

    await _teardown(session)


@pytest.mark.timeout(5)
async def test_a_resize_during_open_sets_the_size_the_room_opens_at() -> None:
    """Same window, same problem: a resize with no room to forward it to."""
    room = FakeRoom()
    chan = FakeChannel()
    backend = SlowBackend(room, delay=0.2)
    session = RoomSession(backend, RoomConfig(name="ubuntu", image="ubuntu:24.04"))
    session.connection_made(chan)  # type: ignore[arg-type]
    session.terminal_size_changed(120, 40, 0, 0)
    session.session_started()

    await asyncio.sleep(0.4)

    assert backend.opened_size == (120, 40)

    await _teardown(session)


@pytest.mark.timeout(5)
async def test_active_set_tracks_session_lifecycle() -> None:
    active = ActiveSessions()
    room = FakeRoom()
    chan = FakeChannel()
    session = RoomSession(
        FakeBackend(room), RoomConfig(name="ubuntu", image="ubuntu:24.04"), active=active
    )
    session.connection_made(chan)  # type: ignore[arg-type]
    assert session in active

    session.session_started()
    await asyncio.sleep(0.05)  # let _run open the room

    # A dropped connection: connection_lost fires with a live room, so
    # release must wait for the async checkout, not happen immediately.
    session.connection_lost(None)
    assert session in active

    await asyncio.sleep(0.05)
    assert room.destroy_count == 1
    assert session not in active

    await _teardown(session)


@pytest.mark.timeout(5)
async def test_notify_shutdown_writes_without_closing() -> None:
    room = FakeRoom()
    chan = FakeChannel()
    session = RoomSession(FakeBackend(room), RoomConfig(name="ubuntu", image="ubuntu:24.04"))
    session.connection_made(chan)  # type: ignore[arg-type]

    session.notify_shutdown(b"keycard: server shutting down, 30s to finish up")

    assert b"".join(chan.written) == b"keycard: server shutting down, 30s to finish up"
    assert chan.exit_status is None
    assert not chan.is_closing()


@pytest.mark.timeout(5)
async def test_force_close_destroys_room_and_releases_session() -> None:
    active = ActiveSessions()
    room = FakeRoom()
    chan = FakeChannel()
    session = RoomSession(
        FakeBackend(room), RoomConfig(name="ubuntu", image="ubuntu:24.04"), active=active
    )
    session.connection_made(chan)  # type: ignore[arg-type]
    session.session_started()
    await asyncio.sleep(0.05)  # let _run open the room

    await session.force_close()

    assert room.destroy_count == 1
    assert chan.exit_status == SHUTDOWN_EXIT_STATUS
    assert chan.is_closing()
    assert b"server shutting down" in b"".join(chan.written)
    assert session not in active

    await _teardown(session)


@pytest.mark.timeout(5)
async def test_force_close_before_room_opens_still_ends_cleanly() -> None:
    """No room to destroy yet — force_close must not blow up on a null room."""
    room = FakeRoom()
    chan = FakeChannel()
    session = RoomSession(FakeBackend(room), RoomConfig(name="ubuntu", image="ubuntu:24.04"))
    session.connection_made(chan)  # type: ignore[arg-type]
    # No session_started(): the room never opens.

    await session.force_close()

    assert room.destroy_count == 0
    assert chan.exit_status == SHUTDOWN_EXIT_STATUS
    assert chan.is_closing()


@pytest.mark.timeout(5)
async def test_drain_of_empty_set_is_a_noop() -> None:
    from keycard.server import _drain

    await _drain(ActiveSessions(), grace=1.0)


@pytest.mark.timeout(5)
async def test_drain_lets_natural_exits_finish_within_grace() -> None:
    """One session hangs up on its own mid-grace; the other has to be forced."""
    from keycard.server import _drain

    active = ActiveSessions()
    room_a, room_b = FakeRoom(), FakeRoom()
    chan_a, chan_b = FakeChannel(), FakeChannel()
    session_a = RoomSession(FakeBackend(room_a), RoomConfig(name="a", image="x"), active=active)
    session_b = RoomSession(FakeBackend(room_b), RoomConfig(name="b", image="x"), active=active)

    for session, chan in ((session_a, chan_a), (session_b, chan_b)):
        session.connection_made(chan)  # type: ignore[arg-type]
        session.session_started()
    await asyncio.sleep(0.03)  # let both rooms open

    async def _disconnect_a() -> None:
        await asyncio.sleep(0.05)
        session_a.connection_lost(None)

    disconnect_task = asyncio.create_task(_disconnect_a())

    await _drain(active, grace=0.3)

    assert room_a.destroy_count == 1
    assert chan_a.exit_status is None  # left on its own, never forced
    assert room_b.destroy_count == 1
    assert chan_b.exit_status == SHUTDOWN_EXIT_STATUS  # still active past the deadline
    assert not active

    await disconnect_task
    await _teardown(session_a)
    await _teardown(session_b)


@pytest.mark.timeout(5)
async def test_drain_with_no_grace_force_closes_immediately() -> None:
    from keycard.server import _drain

    active = ActiveSessions()
    room = FakeRoom()
    chan = FakeChannel()
    session = RoomSession(FakeBackend(room), RoomConfig(name="a", image="x"), active=active)
    session.connection_made(chan)  # type: ignore[arg-type]
    session.session_started()
    await asyncio.sleep(0.03)

    await _drain(active, grace=0.0)

    assert room.destroy_count == 1
    assert chan.exit_status == SHUTDOWN_EXIT_STATUS
    assert not active

    await _teardown(session)


# -- --keep ------------------------------------------------------------------


class FailingResumeBackend(FakeBackend):
    async def resume(self, kept: Kept, width: int, height: int) -> Room:
        raise RuntimeError("boom")


@pytest.mark.timeout(5)
async def test_keep_and_take_roundtrip() -> None:
    room = FakeRoom()
    backend = FakeBackend(room)
    kept = KeptRooms(backend, window_seconds=10.0)

    await kept.keep("alice", room)
    assert room.pause_count == 1

    claimed = kept.take("alice")
    assert claimed is not None
    assert kept.take("alice") is None  # already claimed, nothing left


@pytest.mark.timeout(5)
async def test_kept_room_expires_and_is_destroyed_if_never_reclaimed() -> None:
    room = FakeRoom()
    backend = FakeBackend(room)
    kept = KeptRooms(backend, window_seconds=0.05)

    await kept.keep("alice", room)
    assert kept.take("bob") is None  # different username claims nothing

    await asyncio.sleep(0.15)

    assert room.destroy_kept_count == 1
    assert kept.take("alice") is None  # expired, nothing left to reclaim


@pytest.mark.timeout(5)
async def test_take_cancels_the_expiry_timer() -> None:
    room = FakeRoom()
    backend = FakeBackend(room)
    kept = KeptRooms(backend, window_seconds=0.05)

    await kept.keep("alice", room)
    assert kept.take("alice") is not None

    await asyncio.sleep(0.15)  # past the original window

    assert room.destroy_kept_count == 0  # already claimed; timer was a no-op


@pytest.mark.timeout(5)
async def test_destroy_all_sweeps_every_kept_room() -> None:
    room_a, room_b = FakeRoom(), FakeRoom()
    backend = FakeBackend(room_a)
    kept = KeptRooms(backend, window_seconds=10.0)

    await kept.keep("alice", room_a)
    await kept.keep("bob", room_b)

    await kept.destroy_all()

    assert room_a.destroy_kept_count == 1
    assert room_b.destroy_kept_count == 1
    assert kept.take("alice") is None
    assert kept.take("bob") is None


@pytest.mark.timeout(5)
async def test_dropped_connection_destroys_when_keep_disabled() -> None:
    """No `kept` registry at all — the default, unchanged behaviour."""
    room = FakeRoom()
    session = RoomSession(FakeBackend(room), RoomConfig(name="ubuntu", image="ubuntu:24.04"))
    chan = FakeChannel()
    session.connection_made(chan)  # type: ignore[arg-type]
    session.session_started()
    await asyncio.sleep(0.05)

    session.connection_lost(None)
    await asyncio.sleep(0.05)

    assert room.destroy_count == 1
    assert room.pause_count == 0


@pytest.mark.timeout(5)
async def test_dropped_connection_destroys_when_window_is_zero() -> None:
    """A `kept` registry is wired up, but keep_window="0" — still off."""
    room = FakeRoom()
    backend = FakeBackend(room)
    kept = KeptRooms(backend, window_seconds=0.0)
    assert not kept.enabled

    session = RoomSession(
        backend, RoomConfig(name="ubuntu", image="ubuntu:24.04"), username="alice", kept=kept
    )
    chan = FakeChannel()
    session.connection_made(chan)  # type: ignore[arg-type]
    session.session_started()
    await asyncio.sleep(0.05)

    session.connection_lost(None)
    await asyncio.sleep(0.05)

    assert room.destroy_count == 1
    assert room.pause_count == 0


@pytest.mark.timeout(5)
async def test_dropped_connection_pauses_and_reconnect_resumes() -> None:
    room = FakeRoom()
    backend = FakeBackend(room)
    kept = KeptRooms(backend, window_seconds=10.0)

    chan_a = FakeChannel()
    session_a = RoomSession(
        backend, RoomConfig(name="ubuntu", image="ubuntu:24.04"), username="alice", kept=kept
    )
    session_a.connection_made(chan_a)  # type: ignore[arg-type]
    session_a.session_started()
    await asyncio.sleep(0.05)  # let the room open

    session_a.connection_lost(None)  # dropped mid-session, not a clean exit
    await asyncio.sleep(0.05)  # let the pause complete

    assert room.pause_count == 1
    assert room.destroy_count == 0

    chan_b = FakeChannel()
    session_b = RoomSession(
        backend, RoomConfig(name="ubuntu", image="ubuntu:24.04"), username="alice", kept=kept
    )
    session_b.connection_made(chan_b)  # type: ignore[arg-type]
    session_b.session_started()
    await asyncio.sleep(0.05)

    assert b"KEYCARD RESUMED" in b"".join(chan_b.written)
    assert kept.take("alice") is None  # already claimed by session_b

    await _teardown(session_a)
    await _teardown(session_b)


@pytest.mark.timeout(5)
async def test_resume_failure_destroys_the_claimed_room() -> None:
    """A resume that blows up must not leak the container it already claimed."""
    room = FakeRoom()
    backend = FailingResumeBackend(room)
    kept = KeptRooms(backend, window_seconds=10.0)
    await kept.keep("alice", room)

    chan = FakeChannel()
    session = RoomSession(
        backend, RoomConfig(name="ubuntu", image="ubuntu:24.04"), username="alice", kept=kept
    )
    session.connection_made(chan)  # type: ignore[arg-type]
    session.session_started()
    await asyncio.sleep(0.05)

    assert chan.exit_status == 1
    assert room.destroy_kept_count == 1
    assert kept.take("alice") is None
