"""Backend selection: which implementation opens a room, and who cleans it up.

No real backend is involved — the point of these is the routing, and the
routing has to be right for a `Kept` that outlives the session that made it.
"""

from __future__ import annotations

import pytest

from keycard.backends import routing
from keycard.backends.base import Backend, Kept, Room
from keycard.config import KNOWN_BACKENDS, Config, RoomConfig


class FakeKept(Kept):
    def __init__(self, backend_name: str) -> None:
        self.backend_name = backend_name


class FakeRoom(Room):
    def __init__(self, backend_name: str) -> None:
        self.backend_name = backend_name

    async def read(self) -> bytes:
        return b""

    async def write(self, data: bytes) -> None:
        pass

    async def resize(self, width: int, height: int) -> None:
        pass

    async def destroy(self) -> int:
        return 0

    async def pause(self) -> Kept:
        return FakeKept(self.backend_name)


class FakeBackend(Backend):
    def __init__(self, name: str) -> None:
        self.name = name
        self.opened: list[str] = []
        self.resumed: list[Kept] = []
        self.destroyed: list[Kept] = []
        self.closed = False

    async def open(self, room: RoomConfig, width: int, height: int) -> Room:
        self.opened.append(room.name)
        return FakeRoom(self.name)

    async def resume(self, kept: Kept, width: int, height: int) -> Room:
        self.resumed.append(kept)
        return FakeRoom(self.name)

    async def destroy_kept(self, kept: Kept) -> None:
        self.destroyed.append(kept)

    async def close(self) -> None:
        self.closed = True


@pytest.fixture
def backends() -> dict[str, FakeBackend]:
    return {}


@pytest.fixture
def factories(backends: dict[str, FakeBackend]) -> dict[str, routing.BackendFactory]:
    def make(name: str) -> routing.BackendFactory:
        def factory(config: Config) -> Backend:
            backend = backends[name] = FakeBackend(name)
            return backend

        return factory

    return {name: make(name) for name in ("docker", "firecracker")}


def _config(backend: str = "docker") -> Config:
    """One docker room and one firecracker room — a mixed deployment, which
    is the only interesting shape for a router."""
    rooms = {
        "ubuntu": RoomConfig(name="ubuntu", image="ubuntu:24.04"),
        "vm": RoomConfig(name="vm", image="ubuntu:24.04", backend="firecracker"),
    }
    return Config(rooms=rooms, backend=backend)


def test_registry_matches_the_names_config_will_accept() -> None:
    # config.py validates `backend = "..."` against its own list so that it
    # doesn't have to import a backend to do it. These two drifting apart
    # would mean either a name that loads and then fails, or one that is
    # rejected despite being implemented.
    assert set(routing.FACTORIES) == set(KNOWN_BACKENDS)


async def test_room_without_a_backend_uses_the_server_default(
    factories: dict[str, routing.BackendFactory], backends: dict[str, FakeBackend]
) -> None:
    router = routing.RoutingBackend(_config(backend="docker"), factories)
    await router.open(RoomConfig(name="ubuntu", image="ubuntu:24.04"), 80, 24)
    assert backends["docker"].opened == ["ubuntu"]
    assert backends["firecracker"].opened == []


async def test_room_backend_overrides_the_default(
    factories: dict[str, routing.BackendFactory], backends: dict[str, FakeBackend]
) -> None:
    router = routing.RoutingBackend(_config(backend="docker"), factories)
    room = RoomConfig(name="vm", image="ubuntu:24.04", backend="firecracker")
    await router.open(room, 80, 24)
    assert backends["firecracker"].opened == ["vm"]
    assert backends["docker"].opened == []


async def test_backends_named_in_config_are_built_at_startup(
    factories: dict[str, routing.BackendFactory], backends: dict[str, FakeBackend]
) -> None:
    # An unreachable daemon or a missing guest kernel should stop the server
    # starting, not surface on somebody's first connection.
    routing.RoutingBackend(_config(backend="docker"), factories)
    assert set(backends) == {"docker", "firecracker"}


async def test_unused_backends_are_never_constructed(
    factories: dict[str, routing.BackendFactory], backends: dict[str, FakeBackend]
) -> None:
    config = Config(rooms={"ubuntu": RoomConfig(name="ubuntu", image="ubuntu:24.04")})
    routing.RoutingBackend(config, factories)
    assert set(backends) == {"docker"}


async def test_kept_room_is_destroyed_by_the_backend_that_made_it(
    factories: dict[str, routing.BackendFactory], backends: dict[str, FakeBackend]
) -> None:
    # The whole reason rooms are wrapped: KeptRooms._expire() fires long
    # after the session is gone and has nothing but the Kept to go on.
    router = routing.RoutingBackend(_config(backend="docker"), factories)
    room = await router.open(RoomConfig(name="vm", image="x", backend="firecracker"), 80, 24)
    kept = await room.pause()

    await router.destroy_kept(kept)

    assert len(backends["firecracker"].destroyed) == 1
    assert backends["docker"].destroyed == []


async def test_resume_goes_back_to_the_same_backend(
    factories: dict[str, routing.BackendFactory], backends: dict[str, FakeBackend]
) -> None:
    router = routing.RoutingBackend(_config(backend="docker"), factories)
    room = await router.open(RoomConfig(name="vm", image="x", backend="firecracker"), 80, 24)
    kept = await room.pause()

    resumed = await router.resume(kept, 100, 30)

    assert len(backends["firecracker"].resumed) == 1
    # And the room that comes back is still routed, so pausing it again works.
    assert isinstance(await resumed.pause(), routing.RoutedKept)


async def test_inner_backend_never_sees_the_wrapper(
    factories: dict[str, routing.BackendFactory], backends: dict[str, FakeBackend]
) -> None:
    router = routing.RoutingBackend(_config(backend="docker"), factories)
    room = await router.open(RoomConfig(name="vm", image="x", backend="firecracker"), 80, 24)
    kept = await room.pause()
    assert isinstance(kept, routing.RoutedKept)

    await router.destroy_kept(kept)

    # The tag is the router's business; a backend only ever handles the Kept
    # it produced itself.
    assert isinstance(backends["firecracker"].destroyed[0], FakeKept)


async def test_close_closes_every_backend_it_built(
    factories: dict[str, routing.BackendFactory], backends: dict[str, FakeBackend]
) -> None:
    router = routing.RoutingBackend(_config(backend="docker"), factories)
    await router.close()
    assert all(backend.closed for backend in backends.values())


async def test_one_backend_failing_to_close_does_not_strand_the_others(
    factories: dict[str, routing.BackendFactory], backends: dict[str, FakeBackend]
) -> None:
    router = routing.RoutingBackend(_config(backend="docker"), factories)

    async def boom() -> None:
        raise RuntimeError("nope")

    backends["docker"].close = boom  # type: ignore[method-assign]

    await router.close()

    assert backends["firecracker"].closed


def test_unknown_backend_name_is_refused(
    factories: dict[str, routing.BackendFactory],
) -> None:
    config = Config(rooms={}, backend="qemu")
    with pytest.raises(ValueError, match="unknown backend 'qemu'"):
        routing.RoutingBackend(config, factories)
