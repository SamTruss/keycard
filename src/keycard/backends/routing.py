"""Backend selection.

Rooms can name a backend (``backend = "firecracker"`` under ``[rooms.x]``),
and the top-level ``backend`` key is the default for those that don't. Both,
rather than one or the other, because the expensive part was never the config
key: it is that everything above the seam holds exactly one `Backend`, and
`KeptRooms` calls ``destroy_kept`` on a paused room long after the session
that opened it is gone. Solving that needs a registry that can route a `Kept`
back to whichever backend produced it — and once that exists, a server-level
default is a fallback lookup, three lines.

So the registry is itself a `Backend`. ``server.py`` builds one of these
instead of a `DockerBackend` and nothing downstream learns that there is more
than one implementation, which keeps the promise FIRECRACKER.md made about
the seam: adding a backend changed no code in ``session.py``.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping

from ..config import Config, RoomConfig
from .base import Backend, Kept, Room

log = logging.getLogger(__name__)

BackendFactory = Callable[[Config], Backend]


def _docker(config: Config) -> Backend:
    from .docker import DockerBackend

    return DockerBackend()


def _firecracker(config: Config) -> Backend:
    from .firecracker import FirecrackerBackend

    return FirecrackerBackend(config.firecracker)


# Imports are deferred into the factories so that naming a backend is what
# pays for it: `keycard rooms` on a laptop shouldn't import a microVM stack,
# and a Docker-only deployment shouldn't need one installed.
FACTORIES: Mapping[str, BackendFactory] = {
    "docker": _docker,
    "firecracker": _firecracker,
}


class RoutedKept(Kept):
    """A `Kept` tagged with the backend that made it.

    `Kept` is opaque by contract, so this wraps rather than annotates: the
    backend underneath still only ever sees its own.
    """

    def __init__(self, backend_name: str, inner: Kept) -> None:
        self.backend_name = backend_name
        self.inner = inner


class RoutedRoom(Room):
    """Pass-through, except that `pause` labels what it produces.

    This is the only reason rooms are wrapped at all — it is the one moment a
    backend hands out something that has to find its way home later, and the
    room is the only place that still knows where it came from.
    """

    def __init__(self, backend_name: str, inner: Room) -> None:
        self.backend_name = backend_name
        self.inner = inner

    async def read(self) -> bytes:
        return await self.inner.read()

    async def write(self, data: bytes) -> None:
        await self.inner.write(data)

    async def resize(self, width: int, height: int) -> None:
        await self.inner.resize(width, height)

    async def destroy(self) -> int:
        return await self.inner.destroy()

    async def pause(self) -> Kept:
        return RoutedKept(self.backend_name, await self.inner.pause())


class RoutingBackend(Backend):
    def __init__(self, config: Config, factories: Mapping[str, BackendFactory] = FACTORIES) -> None:
        self._config = config
        self._factories = factories
        self._backends: dict[str, Backend] = {}
        # Built up front, not on first connection: a backend's constructor is
        # where it notices an unreachable Docker daemon or a missing guest
        # kernel, and that has always been a startup failure rather than
        # something a checking-in user discovers.
        for name in sorted(self._names_in_use()):
            self._get(name)

    def _names_in_use(self) -> set[str]:
        names = {self._config.backend}
        names.update(room.backend for room in self._config.rooms.values() if room.backend)
        return names

    def _get(self, name: str) -> Backend:
        backend = self._backends.get(name)
        if backend is not None:
            return backend
        factory = self._factories.get(name)
        if factory is None:
            raise ValueError(
                f"unknown backend {name!r} (known: {', '.join(sorted(self._factories))})"
            )
        backend = self._backends[name] = factory(self._config)
        log.info("backend ready: %s", name)
        return backend

    def _name_for(self, room: RoomConfig) -> str:
        return room.backend or self._config.backend

    async def open(self, room: RoomConfig, width: int, height: int) -> Room:
        name = self._name_for(room)
        return RoutedRoom(name, await self._get(name).open(room, width, height))

    async def resume(self, kept: Kept, width: int, height: int) -> Room:
        routed = _routed(kept)
        inner = await self._get(routed.backend_name).resume(routed.inner, width, height)
        return RoutedRoom(routed.backend_name, inner)

    async def destroy_kept(self, kept: Kept) -> None:
        routed = _routed(kept)
        await self._get(routed.backend_name).destroy_kept(routed.inner)

    async def close(self) -> None:
        # One backend failing to close must not strand the others — this runs
        # once, on the way out, and there is no second attempt.
        for name, backend in self._backends.items():
            try:
                await backend.close()
            except Exception:  # noqa: BLE001 - shutdown is best effort
                log.warning("backend %s did not close cleanly", name, exc_info=True)


def _routed(kept: Kept) -> RoutedKept:
    if not isinstance(kept, RoutedKept):  # pragma: no cover - defensive
        raise TypeError(f"kept room was not produced by this backend: {type(kept).__name__}")
    return kept
