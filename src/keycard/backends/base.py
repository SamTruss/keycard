"""Backend interface.

A backend knows how to build a room, stream bytes to and from it, and destroy
it. Nothing above this layer should know whether a room is a container, a
microVM, or something else — that is the whole point of the seam.
"""

from __future__ import annotations

import abc
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..config import RoomConfig


class Kept:
    """Opaque handle to a room paused by `Room.pause()`.

    Carries whatever a backend needs to find the room again — a container
    ID, a microVM snapshot path, whatever. Nothing above the backend layer
    looks inside it.
    """


class Room(abc.ABC):
    """A single live sandbox with an attached pty."""

    can_pause: bool = True
    """Whether `--keep` may pause this room instead of destroying it.

    A backend sets this False when freezing a room would cost more than the
    reconnect window is worth. Firecracker does: a snapshot writes the whole
    of guest RAM to disk in the clear, and there is no way to take one that
    doesn't, because the VMM restores by reading that file back directly.

    Enforced in `RoomSession._run_teardown`, which destroys instead. It is a
    property of the room rather than of the config because the room is the
    only thing that still knows which backend actually built it — `backend`
    can be set per room or inherited from the top level, and by teardown the
    session no longer has either.
    """

    @abc.abstractmethod
    async def read(self) -> bytes:
        """Return the next chunk of output. Empty bytes means the room ended."""

    @abc.abstractmethod
    async def write(self, data: bytes) -> None:
        """Send input to the room."""

    @abc.abstractmethod
    async def resize(self, width: int, height: int) -> None:
        """Tell the room its terminal changed size."""

    @abc.abstractmethod
    async def destroy(self) -> int:
        """Tear the room down. Returns the exit status, best effort.

        Must be safe to call more than once — checkout can be triggered by the
        client exiting, the connection dropping, or the server shutting down,
        and those can race.
        """

    @abc.abstractmethod
    async def pause(self) -> Kept:
        """Freeze the room instead of destroying it.

        Releases this Room's own per-connection resources (sockets, handles)
        but leaves the sandbox itself intact, so a later `Backend.resume` can
        pick it back up. Used for `--keep`: a dropped connection gets a
        window to reconnect instead of losing its room outright.

        Only called on rooms whose `can_pause` is True.
        """


class Backend(abc.ABC):
    """Builds rooms."""

    can_pause: bool = True
    """Whether rooms from this backend can be paused — see `Room.can_pause`.

    The same answer as the rooms it builds, declared up here so startup can
    ask it before any room exists. `Room.can_pause` is what actually gates
    the pause; this is what lets the server warn about it at boot rather
    than leaving the operator to discover it on their first disconnect.
    A test pins the two together so they cannot drift apart.
    """

    @abc.abstractmethod
    async def open(self, room: RoomConfig, width: int, height: int) -> Room:
        """Create a room and attach to it, applying *room*'s resource caps."""

    @abc.abstractmethod
    async def resume(self, kept: Kept, width: int, height: int) -> Room:
        """Unfreeze a room `pause`d earlier and reattach to it."""

    @abc.abstractmethod
    async def destroy_kept(self, kept: Kept) -> None:
        """Tear down a paused room once its keep window has expired."""

    @abc.abstractmethod
    async def close(self) -> None:
        """Release any backend-level resources."""
