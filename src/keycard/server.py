"""The front desk: an SSH server that hands out rooms."""

from __future__ import annotations

import asyncio
import logging
import signal
import sys
from pathlib import Path

import asyncssh

from .backends.base import Backend
from .backends.routing import RoutingBackend
from .config import Config, RoomConfig
from .session import ActiveSessions, KeptRooms, RoomSession

log = logging.getLogger(__name__)

CONFIG_DIR = Path.home() / ".config" / "keycard"

# Once the grace period is up, force-closed sessions get a moment to actually
# finish their room teardown before shutdown gives up and moves on.
FORCE_CLOSE_TIMEOUT = 5.0


class KeycardServer(asyncssh.SSHServer):
    """One instance per connection."""

    def __init__(
        self,
        backend: Backend,
        config: Config,
        active_sessions: ActiveSessions | None = None,
        kept_rooms: KeptRooms | None = None,
    ) -> None:
        self._backend = backend
        self._config = config
        self._active_sessions = active_sessions
        self._kept_rooms = kept_rooms
        self._peer = "?"
        self._username = ""

    def connection_made(self, conn: asyncssh.SSHServerConnection) -> None:
        peer = conn.get_extra_info("peername")
        self._peer = peer[0] if peer else "?"
        log.info("connection from %s", self._peer)

    def connection_lost(self, exc: Exception | None) -> None:
        log.info("connection closed: %s", self._peer)

    def begin_auth(self, username: str) -> bool:
        self._username = username
        return True

    def password_auth_supported(self) -> bool:
        return False

    def public_key_auth_supported(self) -> bool:
        return True

    def session_requested(self) -> RoomSession:
        room_cfg = self._config.resolve(self._username)
        if room_cfg is None:
            log.warning("no room for username %r", self._username)
            # Fall through — session will fail and report cleanly.
            room_cfg = RoomConfig(name="fallback", image="ubuntu:24.04")
        else:
            log.info("username %r → room %s (%s)", self._username, room_cfg.name, room_cfg.image)
        return RoomSession(
            self._backend,
            room_cfg,
            self._config.idle_timeout_seconds,
            self._active_sessions,
            self._username,
            self._kept_rooms,
        )


def check_authorized_keys(path: Path = CONFIG_DIR / "authorized_keys") -> None:
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


def ensure_host_key(path: Path = CONFIG_DIR / "host_key") -> Path:
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
    config: Config,
    backend: Backend | None = None,
    host_override: str | None = None,
    port_override: int | None = None,
    active_sessions: ActiveSessions | None = None,
    kept_rooms: KeptRooms | None = None,
) -> asyncssh.SSHAcceptor:
    """Start listening and return the acceptor.

    Split out from serve() so tests can drive a real server on an ephemeral
    port without shelling out or blocking forever.
    """
    check_authorized_keys(config.authorized_keys)
    ensure_host_key(config.host_key)
    # Accessed here, not just when used, so a malformed duration string fails
    # fast at startup instead of surfacing later on a connection or at exit.
    log.debug("idle timeout: %.0fs (0 disables the reaper)", config.idle_timeout_seconds)
    log.debug("shutdown grace: %.0fs", config.shutdown_grace_seconds)
    log.debug("keep window: %.0fs (0 disables --keep)", config.keep_window_seconds)
    if backend is None:
        backend = RoutingBackend(config)

    host = host_override if host_override is not None else config.host
    port = port_override if port_override is not None else config.port

    return await asyncssh.create_server(
        lambda: KeycardServer(backend, config, active_sessions, kept_rooms),
        host,
        port,
        server_host_keys=[str(config.host_key)],
        authorized_client_keys=str(config.authorized_keys),
        encoding=None,  # raw bytes; the pty does its own interpreting
        line_editor=False,  # the container shell does its own line editing
    )


async def _drain(active: ActiveSessions, grace: float) -> None:
    """Give connected clients a chance to finish before their rooms disappear.

    ``grace`` <= 0 means no grace at all — sessions are cut immediately, the
    same convention ``docker stop``/``systemctl stop`` use for a 0 timeout.
    """
    if not len(active):
        return

    log.info("draining %d active session(s)", len(active))
    if grace > 0:
        message = f"\r\nkeycard: server shutting down, {grace:.0f}s to finish up\r\n".encode()
        for session in list(active):
            session.notify_shutdown(message)
        try:
            async with asyncio.timeout(grace):
                await active.wait_empty()
        except TimeoutError:
            pass

    if not len(active):
        return

    log.warning("%d session(s) still active after grace period; closing", len(active))
    try:
        await asyncio.wait_for(
            asyncio.gather(*(session.force_close() for session in list(active))),
            timeout=FORCE_CLOSE_TIMEOUT,
        )
    except TimeoutError:
        log.warning("%d session(s) did not close in time; leaving them", len(active))


async def serve(config: Config) -> None:
    backend: Backend = RoutingBackend(config)
    active = ActiveSessions()
    kept = KeptRooms(backend, config.keep_window_seconds)
    server = await create_server(config, backend, active_sessions=active, kept_rooms=kept)

    rooms = ", ".join(f"{r.name} ({r.image})" for r in config.rooms.values())
    log.info("keycard listening on port %s", config.port)
    log.info("rooms: %s (default: %s)", rooms, config.default_room)
    log.info("backend: %s", config.backend)
    log.info("try: ssh -p %s ubuntu@localhost", config.port)
    if kept.enabled:
        log.info("--keep enabled: %.0fs to reconnect after a dropped session", kept.window_seconds)

    loop = asyncio.get_running_loop()
    shutdown_requested = asyncio.Event()
    registered_signals: list[signal.Signals] = []
    # Windows' proactor loop has no signal support; Ctrl-C there falls back to
    # the KeyboardInterrupt catch in cli.py, which skips the drain below.
    # Fine — keycard's server target is Linux/macOS (see SCOPE.md).
    if sys.platform != "win32":
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, shutdown_requested.set)
                registered_signals.append(sig)
            except NotImplementedError:
                pass

    closed = asyncio.ensure_future(server.wait_closed())
    requested = asyncio.ensure_future(shutdown_requested.wait())
    try:
        await asyncio.wait({closed, requested}, return_when=asyncio.FIRST_COMPLETED)
    except asyncio.CancelledError:
        pass
    finally:
        closed.cancel()
        requested.cancel()
        await asyncio.gather(closed, requested, return_exceptions=True)
        for sig in registered_signals:
            loop.remove_signal_handler(sig)

        log.info("shutting down")
        server.close()
        await server.wait_closed()
        await _drain(active, config.shutdown_grace_seconds)
        # Nothing will ever run their expiry timers once this process exits.
        await kept.destroy_all()
        await backend.close()
        log.info("keycard stopped")
