"""Unit tests for RoomSession's pump and idle-timeout reaper (no daemon needed)."""

from __future__ import annotations

import asyncio

import pytest

from keycard.backends.base import Backend, Room
from keycard.config import RoomConfig
from keycard.session import IDLE_EXIT_STATUS, SHUTDOWN_EXIT_STATUS, ActiveSessions, RoomSession


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
