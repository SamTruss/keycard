"""Docker/Podman backend.

The docker SDK is synchronous, so every call into it is pushed onto the default
executor. The attach socket is unwrapped down to a bare socket and driven with
the loop's own socket methods, which avoids a reader thread.

Because the container is created with ``tty=True`` the attach stream is raw —
no 8-byte multiplex header to strip. That is the single biggest simplification
available here, and it is why keycard always allocates a tty.
"""

from __future__ import annotations

import asyncio
import logging
import socket
import time
from typing import Any

import docker
from docker.errors import DockerException, NotFound

from .base import Backend, Room

log = logging.getLogger(__name__)

READ_SIZE = 65536

# How long to let a shell finish exiting on its own before killing it. On a
# clean exit the container is already stopping when we get here; on a dropped
# connection it is still happily running and needs a shove.
EXIT_GRACE = 1.0
EXIT_POLL = 0.05

# Hardening applied to every room. None of this makes a container a security
# boundary — see SECURITY.md — but it removes the obvious footguns.
ROOM_DEFAULTS: dict[str, Any] = {
    "command": ["/bin/sh", "-c", "exec bash -l 2>/dev/null || exec sh -l"],
    "entrypoint": "",
    "environment": {"TERM": "xterm-256color"},
    "tty": True,
    "stdin_open": True,
    "detach": True,
    "network_disabled": False,
    "cap_drop": ["ALL"],
    "security_opt": ["no-new-privileges:true"],
    "mem_limit": "1g",
    "pids_limit": 512,
}


class DockerRoom(Room):
    def __init__(self, container: Any, sock: socket.socket, raw: Any) -> None:
        self._container = container
        self._sock = sock
        self._raw = raw
        self._loop = asyncio.get_running_loop()
        self._destroyed = False
        self._lock = asyncio.Lock()

    async def read(self) -> bytes:
        try:
            return await self._loop.sock_recv(self._sock, READ_SIZE)
        except (OSError, asyncio.CancelledError):
            return b""

    async def write(self, data: bytes) -> None:
        try:
            await self._loop.sock_sendall(self._sock, data)
        except OSError:
            log.debug("write to room failed; it has probably gone")

    async def resize(self, width: int, height: int) -> None:
        def _resize() -> None:
            try:
                self._container.resize(height=height, width=width)
            except (DockerException, NotFound):
                log.debug("resize failed; room may have ended")

        await self._loop.run_in_executor(None, _resize)

    async def destroy(self) -> int:
        async with self._lock:
            if self._destroyed:
                return 0
            self._destroyed = True

        def _destroy() -> int:
            # Deliberately not container.wait(): its timeout is the HTTP read
            # timeout, so a still-running shell makes it block and then raise.
            # Polling the state is cheap and gives us the real exit code.
            status = 0
            deadline = time.monotonic() + EXIT_GRACE
            try:
                while True:
                    self._container.reload()
                    state = self._container.attrs.get("State", {})
                    if not state.get("Running", False):
                        status = int(state.get("ExitCode") or 0)
                        break
                    if time.monotonic() >= deadline:
                        # Connection dropped with the shell still alive.
                        self._container.kill()
                        break
                    time.sleep(EXIT_POLL)
            except NotFound:
                log.debug("container already gone")
            except DockerException:
                log.debug("could not read container state", exc_info=True)

            try:
                self._container.remove(force=True)
            except (DockerException, NotFound):
                log.debug("container already removed")
            return status

        try:
            self._sock.close()
        except OSError:
            log.debug("socket already closed")
        try:
            self._raw.close()
        except Exception:  # noqa: BLE001
            log.debug("attach handle already closed")

        return await self._loop.run_in_executor(None, _destroy)


class DockerBackend(Backend):
    def __init__(self) -> None:
        self._client = docker.from_env()

    async def open(self, image: str, width: int, height: int) -> Room:
        loop = asyncio.get_running_loop()

        def _open() -> tuple[Any, socket.socket, Any]:
            container = self._client.containers.run(image, **ROOM_DEFAULTS)
            try:
                container.resize(height=height, width=width)
            except DockerException:
                log.debug("initial resize failed")

            raw = container.attach_socket(
                params={"stdin": 1, "stdout": 1, "stderr": 1, "stream": 1}
            )
            # docker-py hands back a wrapper; the bare socket is what the event
            # loop can actually poll.
            sock = getattr(raw, "_sock", raw)
            sock.setblocking(False)
            return container, sock, raw

        container, sock, raw = await loop.run_in_executor(None, _open)
        log.info("room opened: %s (%s)", container.short_id, image)
        return DockerRoom(container, sock, raw)

    async def close(self) -> None:
        await asyncio.get_running_loop().run_in_executor(None, self._client.close)
