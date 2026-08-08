"""Tests for the bits that do not need a container runtime."""

import sys
from pathlib import Path

import asyncssh
import pytest

from keycard.config import Config
from keycard.server import KeycardServer, check_authorized_keys, ensure_host_key


def test_missing_authorized_keys_explains_the_fix(tmp_path: Path) -> None:
    missing = tmp_path / "authorized_keys"
    with pytest.raises(FileNotFoundError) as exc:
        check_authorized_keys(missing)
    # The error should be actionable, not just "file not found".
    assert "cp ~/.ssh/id_ed25519.pub" in str(exc.value)


def test_authorized_keys_present_passes(tmp_path: Path) -> None:
    keys = tmp_path / "authorized_keys"
    keys.write_text("ssh-ed25519 AAAA... test\n")
    check_authorized_keys(keys)


def test_host_key_generated_once_and_locked_down(tmp_path: Path) -> None:
    path = tmp_path / "host_key"
    ensure_host_key(path)

    assert path.exists()
    assert path.with_suffix(".pub").exists()
    if sys.platform != "win32":
        # Windows has no POSIX mode bits; chmod only toggles the read-only
        # flag there. keycard serves from Linux/macOS, so this is the platform
        # that matters — Windows is a dev-only environment.
        assert path.stat().st_mode & 0o777 == 0o600
    # It must load as a real key, not just be a file we made.
    asyncssh.read_private_key(str(path))

    # Regenerating would break every client with a host key mismatch.
    before = path.read_bytes()
    ensure_host_key(path)
    assert path.read_bytes() == before


def test_auth_is_always_required() -> None:
    cfg = Config()
    server = KeycardServer(backend=None, config=cfg)  # type: ignore[arg-type]
    # begin_auth returning False would let anyone in without a key.
    assert server.begin_auth("guest") is True
    assert server.password_auth_supported() is False
    assert server.public_key_auth_supported() is True


# -- shutdown ordering -------------------------------------------------------


@pytest.mark.timeout(10)
async def test_shutdown_drains_before_waiting_for_connections_to_close() -> None:
    """The drain must come before `wait_closed()`, not after.

    `asyncio.Server.wait_closed()` does not return until every accepted
    connection has dropped as well as the listener, so awaiting it first
    deadlocks shutdown against `_drain` — the one thing that closes those
    connections. The server then sits there with rooms still running until
    the last client happens to disconnect on its own, and `shutdown_grace`
    means nothing.

    Asserted as ordering because the deadlock's natural failure mode is a
    hang, and a test that hangs teaches nobody anything. This was invisible
    on Python 3.11, where `wait_closed()` returned immediately once the
    listener was closed — so it only bites from 3.12.1 onward.
    """
    import asyncio

    from keycard import server as server_mod
    from keycard.backends.base import Backend

    order: list[str] = []

    class _FakeAcceptor:
        """Returns from the first `wait_closed()` so `serve()` moves on to
        its shutdown block, the same way a closed listener would."""

        def __init__(self) -> None:
            self.waits = 0

        def close(self) -> None:
            order.append("listener closed")

        async def wait_closed(self) -> None:
            self.waits += 1
            order.append(f"wait_closed#{self.waits}")

    class _FakeBackend(Backend):
        async def open(self, room, width, height):  # type: ignore[no-untyped-def]
            raise AssertionError("no room should be opened")

        async def resume(self, kept, width, height):  # type: ignore[no-untyped-def]
            raise AssertionError("no room should be resumed")

        async def destroy_kept(self, kept) -> None:  # type: ignore[no-untyped-def]
            pass

        async def close(self) -> None:
            order.append("backend closed")

    async def _fake_drain(active, grace):  # type: ignore[no-untyped-def]
        order.append("drained")

    async def _fake_create_server(*args, **kwargs):  # type: ignore[no-untyped-def]
        return _FakeAcceptor()

    saved = (server_mod.RoutingBackend, server_mod.create_server, server_mod._drain)
    server_mod.RoutingBackend = lambda config: _FakeBackend()  # type: ignore[assignment,misc]
    server_mod.create_server = _fake_create_server  # type: ignore[assignment]
    server_mod._drain = _fake_drain  # type: ignore[assignment]
    try:
        await asyncio.wait_for(server_mod.serve(Config()), timeout=5)
    finally:
        server_mod.RoutingBackend, server_mod.create_server, server_mod._drain = saved  # type: ignore[assignment,misc]

    shutdown_began = order.index("listener closed")
    after = order[shutdown_began:]
    assert "drained" in after, order
    assert after.index("drained") < after.index("wait_closed#2"), order
