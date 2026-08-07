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


class Backend(abc.ABC):
    """Builds rooms."""

    @abc.abstractmethod
    async def open(self, room: RoomConfig, width: int, height: int) -> Room:
        """Create a room and attach to it, applying *room*'s resource caps."""

    @abc.abstractmethod
    async def close(self) -> None:
        """Release any backend-level resources."""
