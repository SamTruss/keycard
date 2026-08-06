"""The bridge between an SSH channel and a room's pty.

This is implemented as an ``SSHServerSession`` rather than via asyncssh's
higher-level process API because we need ``terminal_size_changed``. Window
resize is the thing that makes a session feel real, and the process API does
not surface it.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import asyncssh

from .backends.base import Backend, Room

log = logging.getLogger(__name__)


class RoomSession(asyncssh.SSHServerSession[bytes]):
    def __init__(self, backend: Backend, image: str) -> None:
        self._backend = backend
        self._image = image
        self._chan: asyncssh.SSHServerChannel[bytes] | None = None
        self._room: Room | None = None
        self._pump: asyncio.Task[None] | None = None
        self._size = (80, 24)

    # -- channel lifecycle -------------------------------------------------

    def connection_made(self, chan: asyncssh.SSHServerChannel[bytes]) -> None:
        self._chan = chan

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

    def terminal_size_changed(self, width: int, height: int, pixwidth: int, pixheight: int) -> None:
        if self._room is not None:
            asyncio.create_task(self._room.resize(width, height))  # noqa: RUF006

    def data_received(self, data: bytes, datatype: int | None) -> None:
        if self._room is not None:
            asyncio.create_task(self._room.write(data))  # noqa: RUF006

    def eof_received(self) -> bool:
        return False

    def connection_lost(self, exc: Exception | None) -> None:
        # Checkout. This fires whether the user typed exit or the network died,
        # which is exactly the guarantee keycard is selling.
        if self._pump is not None:
            self._pump.cancel()
        if self._room is not None:
            asyncio.create_task(self._checkout(self._room))  # noqa: RUF006
            self._room = None

    # -- the pump ----------------------------------------------------------

    async def _run(self) -> None:
        chan = self._chan
        if chan is None:  # pragma: no cover - connection_made always runs first
            return
        try:
            self._room = await self._backend.open(self._image, *self._size)
        except Exception:
            log.exception("could not open room")
            chan.write(b"keycard: no room available\r\n")
            chan.exit(1)
            return

        room = self._room
        try:
            while True:
                data = await room.read()
                if not data:
                    break
                chan.write(data)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("room stream failed")
        finally:
            if self._room is not None:
                status = await self._checkout(room)
                self._room = None
                if not chan.is_closing():
                    chan.exit(status)

    async def _checkout(self, room: Room) -> int:
        try:
            status = await room.destroy()
            log.info("room destroyed (status %s)", status)
            return status
        except Exception:
            log.exception("checkout failed")
            return 1
