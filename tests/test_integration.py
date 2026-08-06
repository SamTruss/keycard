"""End-to-end tests against a real container runtime.

These replace the three-terminal manual dance: they start a real keycard
server on an ephemeral port, connect a real SSH client, and check what
happened to the container afterwards.

Skipped automatically when no daemon is reachable, so `pytest` stays green on
a machine without Docker.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path

import asyncssh
import pytest

from keycard.backends.docker import DockerBackend
from keycard.server import create_server

IMAGE = "ubuntu:24.04"


def _docker_available() -> bool:
    try:
        import docker

        docker.from_env().ping()
    except Exception:  # noqa: BLE001 - any failure means "no runtime"
        return False
    return True


pytestmark = pytest.mark.skipif(
    not _docker_available(), reason="needs a reachable Docker/Podman daemon"
)


def _room_ids() -> set[str]:
    """Containers built from our image, however they were left."""
    import docker

    client = docker.from_env()
    return {
        c.id
        for c in client.containers.list(all=True)
        if IMAGE in (c.image.tags or []) or c.attrs.get("Config", {}).get("Image") == IMAGE
    }


@pytest.fixture
async def keycard_server(tmp_path: Path) -> AsyncIterator[tuple[int, Path]]:
    """A real server on an ephemeral port, with a throwaway client key."""
    client_key = asyncssh.generate_private_key("ssh-ed25519")
    key_path = tmp_path / "id_ed25519"
    key_path.write_bytes(client_key.export_private_key())

    authorized = tmp_path / "authorized_keys"
    authorized.write_bytes(client_key.export_public_key())

    backend = DockerBackend()
    server = await create_server(
        host="127.0.0.1",
        port=0,
        image=IMAGE,
        authorized_keys=authorized,
        host_key=tmp_path / "host_key",
        backend=backend,
    )
    port = next(iter(server.sockets)).getsockname()[1]
    try:
        yield port, key_path
    finally:
        server.close()
        await backend.close()


async def _shell(port: int, key: Path) -> asyncssh.SSHClientConnection:
    return await asyncssh.connect(
        "127.0.0.1",
        port=port,
        client_keys=[str(key)],
        known_hosts=None,
        username="guest",
    )


async def test_clean_exit_reports_status_and_destroys_room(
    keycard_server: tuple[int, Path],
) -> None:
    port, key = keycard_server
    before = _room_ids()

    async with await _shell(port, key) as conn:
        proc = await conn.create_process(term_type="xterm", term_size=(80, 24))
        proc.stdin.write("exit 3\n")
        result = await proc.wait()

    # The real exit code must survive the bridge, not be flattened to zero.
    assert result.exit_status == 3

    await asyncio.sleep(1)
    assert _room_ids() == before, "room leaked after a clean exit"


async def test_dropped_connection_destroys_room(
    keycard_server: tuple[int, Path],
) -> None:
    port, key = keycard_server
    before = _room_ids()

    conn = await _shell(port, key)
    proc = await conn.create_process(term_type="xterm", term_size=(80, 24))
    proc.stdin.write("sleep 300\n")
    await asyncio.sleep(2)  # let the room come up and the shell get busy

    assert _room_ids() != before, "no room was created"

    # Yank the connection with the shell still running — the promise keycard
    # is actually selling.
    conn.abort()
    await conn.wait_closed()

    await asyncio.sleep(3)
    assert _room_ids() == before, "room leaked after a dropped connection"


async def test_unauthorized_key_is_refused(keycard_server: tuple[int, Path]) -> None:
    port, _ = keycard_server
    stranger = asyncssh.generate_private_key("ssh-ed25519")

    with pytest.raises(asyncssh.PermissionDenied):
        await asyncssh.connect(
            "127.0.0.1",
            port=port,
            client_keys=[stranger],
            known_hosts=None,
            username="guest",
        )


async def test_resize_is_accepted(keycard_server: tuple[int, Path]) -> None:
    port, key = keycard_server

    async with await _shell(port, key) as conn:
        proc = await conn.create_process(term_type="xterm", term_size=(80, 24))
        proc.change_terminal_size(120, 40)
        await asyncio.sleep(1)
        proc.stdin.write("exit 0\n")
        result = await proc.wait()

    assert result.exit_status == 0
