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
        """


class Backend(abc.ABC):
    """Builds rooms."""

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
