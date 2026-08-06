"""The front desk: an SSH server that hands out rooms."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

import asyncssh

from .backends.base import Backend
from .backends.docker import DockerBackend
from .session import RoomSession

log = logging.getLogger(__name__)

CONFIG_DIR = Path.home() / ".config" / "keycard"
HOST_KEY = CONFIG_DIR / "host_key"
AUTHORIZED_KEYS = CONFIG_DIR / "authorized_keys"


class KeycardServer(asyncssh.SSHServer):
    """One instance per connection."""

    def __init__(self, backend: Backend, image: str) -> None:
        self._backend = backend
        self._image = image
        self._peer = "?"

    def connection_made(self, conn: asyncssh.SSHServerConnection) -> None:
        peer = conn.get_extra_info("peername")
        self._peer = peer[0] if peer else "?"
        log.info("connection from %s", self._peer)

    def connection_lost(self, exc: Exception | None) -> None:
        log.info("connection closed: %s", self._peer)

    def begin_auth(self, username: str) -> bool:
        # True means "authentication is required". Never return False here:
        # that would let anyone in without a key.
        return True

    def password_auth_supported(self) -> bool:
        return False

    def public_key_auth_supported(self) -> bool:
        return True

    def session_requested(self) -> RoomSession:
        return RoomSession(self._backend, self._image)


def check_authorized_keys(path: Path = AUTHORIZED_KEYS) -> None:
    """Fail fast, with the exact commands to fix it.

    Kept synchronous and called before the loop starts: an empty or missing
    key file means nobody can ever check in, so there is no point listening.
    """
    if not path.exists():
        raise FileNotFoundError(
            f"no authorized_keys at {path}\n"
            f"  mkdir -p {path.parent}\n"
            f"  cp ~/.ssh/id_ed25519.pub {path}"
        )


def ensure_host_key(path: Path = HOST_KEY) -> Path:
    """Generate a host key on first run.

    Written 0600 with the private key never leaving this directory. If the key
    changes, every client sees a host-key-mismatch warning, so it is generated
    once and kept.
    """
    if path.exists():
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    key = asyncssh.generate_private_key("ssh-ed25519", comment="keycard host key")
    path.write_bytes(key.export_private_key())
    path.chmod(0o600)
    path.with_suffix(".pub").write_bytes(key.export_public_key())
    log.info("generated host key at %s", path)
    return path


async def create_server(
    host: str = "",
    port: int = 2222,
    image: str = "ubuntu:24.04",
    authorized_keys: Path = AUTHORIZED_KEYS,
    host_key: Path = HOST_KEY,
    backend: Backend | None = None,
) -> asyncssh.SSHAcceptor:
    """Start listening and return the acceptor.

    Split out from serve() so tests can drive a real server on an ephemeral
    port without shelling out or blocking forever.
    """
    check_authorized_keys(authorized_keys)
    ensure_host_key(host_key)
    if backend is None:
        backend = DockerBackend()

    return await asyncssh.create_server(
        lambda: KeycardServer(backend, image),
        host,
        port,
        server_host_keys=[str(host_key)],
        authorized_client_keys=str(authorized_keys),
        encoding=None,  # raw bytes; the pty does its own interpreting
        line_editor=False,  # the container shell does its own line editing
    )


async def serve(
    host: str = "",
    port: int = 2222,
    image: str = "ubuntu:24.04",
    authorized_keys: Path = AUTHORIZED_KEYS,
    host_key: Path = HOST_KEY,
) -> None:
    backend: Backend = DockerBackend()
    server = await create_server(host, port, image, authorized_keys, host_key, backend)

    log.info("keycard listening on port %s, serving %s", port, image)
    log.info("try: ssh -p %s guest@localhost", port)

    try:
        await server.wait_closed()
    except asyncio.CancelledError:
        pass
    finally:
        server.close()
        await backend.close()
