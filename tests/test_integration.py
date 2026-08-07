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
from keycard.config import Config, RoomConfig
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


@pytest.fixture(scope="session", autouse=True)
def _pull_test_image() -> None:
    """Pull `IMAGE` once, outside any single test's timing budget.

    On a cold runner (a fresh CI VM, or anyone's first local run) the first
    `containers.run()` pulls the image inline, which can easily take longer
    than a test allows for the shell to start — input sent before the room
    finishes opening is silently dropped, so the shell never sees `exit` and
    the test hangs until its own timeout. Pulling up front makes every
    test's timing budget only ever about the container, never the network.
    """
    if not _docker_available():
        return
    import docker

    docker.from_env().images.pull(IMAGE)


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

    cfg = Config(
        authorized_keys=authorized,
        host_key=tmp_path / "host_key",
        rooms={"ubuntu": RoomConfig(name="ubuntu", image=IMAGE)},
        default_room="ubuntu",
    )

    backend = DockerBackend()
    server = await create_server(
        cfg,
        backend=backend,
        host_override="127.0.0.1",
        port_override=0,
    )
    port = next(iter(server.sockets)).getsockname()[1]
    try:
        yield port, key_path
    finally:
        server.close()
        await backend.close()


@pytest.fixture
async def capped_keycard_server(tmp_path: Path) -> AsyncIterator[tuple[int, Path]]:
    """A server with one room carrying explicit resource caps and no network."""
    client_key = asyncssh.generate_private_key("ssh-ed25519")
    key_path = tmp_path / "id_ed25519"
    key_path.write_bytes(client_key.export_private_key())

    authorized = tmp_path / "authorized_keys"
    authorized.write_bytes(client_key.export_public_key())

    cfg = Config(
        authorized_keys=authorized,
        host_key=tmp_path / "host_key",
        rooms={
            "capped": RoomConfig(
                name="capped",
                image=IMAGE,
                memory="128m",
                cpus=1,
                pids_limit=64,
                network="none",
            )
        },
        default_room="capped",
    )

    backend = DockerBackend()
    server = await create_server(
        cfg,
        backend=backend,
        host_override="127.0.0.1",
        port_override=0,
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


@pytest.mark.timeout(30)
async def test_clean_exit_reports_status_and_destroys_room(
    keycard_server: tuple[int, Path],
) -> None:
    port, key = keycard_server
    before = _room_ids()

    async with await _shell(port, key) as conn:
        proc = await conn.create_process(term_type="xterm", term_size=(80, 24))
        await asyncio.sleep(1)  # let the shell start
        proc.stdin.write("exit 3\n")
        result = await asyncio.wait_for(proc.wait(), timeout=10)

    assert result.exit_status == 3

    await asyncio.sleep(2)
    assert _room_ids() == before, "room leaked after a clean exit"


@pytest.mark.timeout(30)
async def test_dropped_connection_destroys_room(
    keycard_server: tuple[int, Path],
) -> None:
    port, key = keycard_server
    before = _room_ids()

    conn = await _shell(port, key)
    proc = await conn.create_process(term_type="xterm", term_size=(80, 24))
    await asyncio.sleep(2)  # let the shell start
    proc.stdin.write("sleep 300\n")
    await asyncio.sleep(2)  # let the room come up and the shell get busy

    assert _room_ids() != before, "no room was created"

    conn.abort()
    await asyncio.wait_for(conn.wait_closed(), timeout=10)

    await asyncio.sleep(3)
    assert _room_ids() == before, "room leaked after a dropped connection"


@pytest.mark.timeout(15)
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


@pytest.mark.timeout(30)
async def test_resize_is_accepted(keycard_server: tuple[int, Path]) -> None:
    port, key = keycard_server

    async with await _shell(port, key) as conn:
        proc = await conn.create_process(term_type="xterm", term_size=(80, 24))
        await asyncio.sleep(1)
        proc.change_terminal_size(120, 40)
        await asyncio.sleep(1)
        proc.stdin.write("exit 0\n")
        result = await asyncio.wait_for(proc.wait(), timeout=10)

    assert result.exit_status == 0


@pytest.mark.timeout(60)
async def test_concurrent_sessions_get_isolated_rooms_and_all_clean_up(
    keycard_server: tuple[int, Path],
) -> None:
    port, key = keycard_server
    before = _room_ids()
    concurrency = 5
    go = asyncio.Event()

    async def _session(i: int) -> int:
        async with await _shell(port, key) as conn:
            proc = await conn.create_process(term_type="xterm", term_size=(80, 24))
            await asyncio.sleep(1)  # let the shell start
            await go.wait()  # hold every session open until they've all peaked together
            proc.stdin.write(f"exit {i}\n")
            result = await asyncio.wait_for(proc.wait(), timeout=15)
            return result.exit_status

    tasks = [asyncio.create_task(_session(i)) for i in range(concurrency)]

    # Prove the server actually runs connections in parallel rather than
    # one at a time: every session's room should be live at once.
    await asyncio.sleep(2)
    peak = _room_ids() - before
    assert len(peak) == concurrency, f"expected {concurrency} concurrent rooms, saw {len(peak)}"

    go.set()
    statuses = await asyncio.wait_for(asyncio.gather(*tasks), timeout=20)

    # Each session's own exit code came back to the right connection —
    # nothing crossed wires between the concurrent rooms.
    assert statuses == list(range(concurrency))

    await asyncio.sleep(2)
    assert _room_ids() == before, "a room leaked after concurrent sessions disconnected"


@pytest.mark.timeout(30)
async def test_per_room_caps_and_network_isolation_are_applied(
    capped_keycard_server: tuple[int, Path],
) -> None:
    import docker

    port, key = capped_keycard_server
    client = docker.from_env()

    async with await _shell(port, key) as conn:
        proc = await conn.create_process(term_type="xterm", term_size=(80, 24))
        await asyncio.sleep(1)  # let the room come up

        containers = [
            c for c in client.containers.list() if c.attrs.get("Config", {}).get("Image") == IMAGE
        ]
        assert len(containers) == 1, "expected exactly one live capped room"
        host_config = containers[0].attrs["HostConfig"]

        assert host_config["Memory"] == 128 * 1024 * 1024
        assert host_config["NanoCpus"] == 1_000_000_000
        assert host_config["PidsLimit"] == 64
        assert host_config["NetworkMode"] == "none"

        proc.stdin.write("exit 0\n")
        await asyncio.wait_for(proc.wait(), timeout=10)
