"""Unit tests for RoomSession's pump and idle-timeout reaper (no daemon needed)."""

from __future__ import annotations

import asyncio

import pytest

from keycard.backends.base import Backend, Room
from keycard.config import RoomConfig
from keycard.session import IDLE_EXIT_STATUS, RoomSession


class FakeRoom(Room):
    def __init__(self) -> None:
        self.destroy_count = 0
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


class FakeBackend(Backend):
    def __init__(self, room: FakeRoom) -> None:
        self._room = room

    async def open(self, room: RoomConfig, width: int, height: int) -> Room:
        return self._room

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


async def _teardown(session: RoomSession) -> None:
    """Cancel background tasks so a test doesn't leak them past its scope."""
    tasks = [t for t in (session._pump, session._watchdog) if t is not None]
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
