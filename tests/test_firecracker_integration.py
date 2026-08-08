"""End-to-end tests against real microVMs.

These are the ones that cannot be faked. FIRECRACKER.md is explicit about
why: mocking the KVM boundary away to make a test pass on a runner without
``/dev/kvm`` would be testing everything except the thing this backend
exists for.

So they skip unless the host can actually boot one, exactly as
``test_integration.py`` skips without a Docker daemon. Nothing in this file
has ever run — see the checklist in FIRECRACKER.md for what a host needs
before it can.

    export KEYCARD_TEST_KERNEL=/var/lib/keycard/vmlinux
    export KEYCARD_TEST_ROOTFS_DIR=/var/lib/keycard/rootfs
    pytest tests/test_firecracker_integration.py -v
"""

from __future__ import annotations

import asyncio
import os
import shutil
from collections.abc import AsyncIterator
from pathlib import Path

import asyncssh
import pytest

from keycard.backends.base import Room
from keycard.backends.firecracker import FirecrackerBackend, FirecrackerKept, kvm_present
from keycard.config import Config, FirecrackerConfig, RoomConfig
from keycard.server import create_server

ROOM = "ubuntu"

# How long to give a microVM to boot far enough to answer. Generous: a first
# boot on a cold page cache is not a failed boot.
BOOT_TIMEOUT = "60s"


def _kernel() -> Path | None:
    raw = os.environ.get("KEYCARD_TEST_KERNEL")
    return Path(raw) if raw else None


def _rootfs_dir() -> Path:
    return Path(os.environ.get("KEYCARD_TEST_ROOTFS_DIR", "/var/lib/keycard/rootfs"))


def _firecracker_available() -> str:
    """Return why these can't run, or "" if they can.

    A reason rather than a bool: "skipped" with no explanation is how a
    backend quietly stops being tested at all.
    """
    if not kvm_present():
        return "no /dev/kvm — needs bare metal or nested virtualization"
    if not os.access("/dev/kvm", os.R_OK | os.W_OK):
        return "no read/write on /dev/kvm — add this user to the kvm group"
    if shutil.which("firecracker") is None:
        return "firecracker is not on PATH"
    kernel = _kernel()
    if kernel is None:
        return "set KEYCARD_TEST_KERNEL to a guest kernel image"
    if not kernel.is_file():
        return f"no guest kernel at {kernel}"
    if not (_rootfs_dir() / f"{ROOM}.ext4").is_file():
        return f"no {ROOM}.ext4 in {_rootfs_dir()} — run `sudo rootfs/build.sh --room {ROOM}`"
    return ""


_SKIP_REASON = _firecracker_available()
pytestmark = [
    pytest.mark.skipif(bool(_SKIP_REASON), reason=_SKIP_REASON or "runnable"),
    pytest.mark.timeout(180),
]


def _config(tmp_path: Path, **room_kwargs: object) -> Config:
    kernel = _kernel()
    assert kernel is not None  # guarded by the skip above
    return Config(
        rooms={ROOM: RoomConfig(name=ROOM, image="ubuntu:24.04", **room_kwargs)},  # type: ignore[arg-type]
        default_room=ROOM,
        backend="firecracker",
        keep_window="5m",
        firecracker=FirecrackerConfig(
            kernel=kernel,
            rootfs_dir=_rootfs_dir(),
            runtime_dir=tmp_path / "run",
            boot_timeout=BOOT_TIMEOUT,
        ),
    )


@pytest.fixture
async def backend(tmp_path: Path) -> AsyncIterator[FirecrackerBackend]:
    made = FirecrackerBackend(_config(tmp_path).firecracker)
    try:
        yield made
    finally:
        await made.close()


# -- the backend on its own -------------------------------------------------


async def test_a_room_boots_and_the_shell_answers(
    backend: FirecrackerBackend, tmp_path: Path
) -> None:
    room = await backend.open(RoomConfig(name=ROOM, image="ubuntu:24.04"), 80, 24)
    try:
        await room.write(b"echo hello-from-microvm\n")
        assert b"hello-from-microvm" in await _read_until(room, b"hello-from-microvm")
    finally:
        await room.destroy()


async def test_resize_reaches_the_guest_pty(backend: FirecrackerBackend) -> None:
    # The control channel's whole purpose. `stty size` prints rows then cols.
    room = await backend.open(RoomConfig(name=ROOM, image="ubuntu:24.04"), 120, 40)
    try:
        await room.resize(120, 40)
        await room.write(b"stty size\n")
        assert b"40 120" in await _read_until(room, b"40 120")
    finally:
        await room.destroy()


async def test_exit_status_comes_back_from_the_guest(backend: FirecrackerBackend) -> None:
    room = await backend.open(RoomConfig(name=ROOM, image="ubuntu:24.04"), 80, 24)
    await room.write(b"exit 7\n")
    # Drain until the agent closes the data channel, which is how a room ends.
    while await room.read():
        pass
    assert await room.destroy() == 7


async def test_destroy_leaves_nothing_behind(backend: FirecrackerBackend, tmp_path: Path) -> None:
    room = await backend.open(RoomConfig(name=ROOM, image="ubuntu:24.04"), 80, 24)
    runtime_dir = tmp_path / "run"
    assert list(runtime_dir.iterdir())
    await room.destroy()
    assert not list(runtime_dir.iterdir())


async def test_destroy_is_idempotent(backend: FirecrackerBackend) -> None:
    # Checkout races: the client exiting, the connection dropping and the
    # server shutting down can all reach destroy() at once.
    room = await backend.open(RoomConfig(name=ROOM, image="ubuntu:24.04"), 80, 24)
    first, second = await asyncio.gather(room.destroy(), room.destroy())
    # Whichever call loses the race returns 0 rather than raising or tearing
    # the same microVM down twice.
    assert 0 in (first, second)


# -- pause / resume ---------------------------------------------------------


async def test_a_paused_room_comes_back_with_its_state(backend: FirecrackerBackend) -> None:
    """The point of `--keep`: the shell is the same shell, not a new one."""
    room = await backend.open(RoomConfig(name=ROOM, image="ubuntu:24.04"), 80, 24)
    await room.write(b"MARKER=still-here\n")
    await _read_until(room, b"MARKER=still-here")

    kept = await room.pause()
    resumed = await backend.resume(kept, 80, 24)
    try:
        await resumed.write(b"echo $MARKER\n")
        assert b"still-here" in await _read_until(resumed, b"still-here")
    finally:
        await resumed.destroy()


async def test_pause_writes_a_snapshot_and_frees_the_process(
    backend: FirecrackerBackend, tmp_path: Path
) -> None:
    room = await backend.open(RoomConfig(name=ROOM, image="ubuntu:24.04"), 80, 24)
    kept = await room.pause()
    assert isinstance(kept, FirecrackerKept)
    assert kept.instance.snapshot.is_file()
    assert kept.instance.mem_file.is_file()
    await backend.destroy_kept(kept)


async def test_destroy_kept_actually_frees_the_snapshot_disk(
    backend: FirecrackerBackend, tmp_path: Path
) -> None:
    # FIRECRACKER.md's open question: a Firecracker snapshot is real disk, so
    # the keep window expiring has to reclaim it, not just stop a process.
    room = await backend.open(RoomConfig(name=ROOM, image="ubuntu:24.04"), 80, 24)
    kept = await room.pause()
    assert isinstance(kept, FirecrackerKept)
    assert kept.instance.dir.is_dir()

    await backend.destroy_kept(kept)

    assert not kept.instance.dir.exists()
    assert not list((tmp_path / "run").iterdir())


# -- through a real SSH session ---------------------------------------------


@pytest.fixture
async def keycard_server(tmp_path: Path) -> AsyncIterator[tuple[int, Path]]:
    """A real server on an ephemeral port, backed by microVMs."""
    client_key = asyncssh.generate_private_key("ssh-ed25519")
    key_path = tmp_path / "id_ed25519"
    key_path.write_bytes(client_key.export_private_key())

    authorized = tmp_path / "authorized_keys"
    authorized.write_bytes(client_key.export_public_key())

    cfg = _config(tmp_path)
    cfg.authorized_keys = authorized
    cfg.host_key = tmp_path / "host_key"

    backend = FirecrackerBackend(cfg.firecracker)
    server = await create_server(cfg, backend=backend, host_override="127.0.0.1", port_override=0)
    port = next(iter(server.sockets)).getsockname()[1]
    try:
        yield port, key_path
    finally:
        server.close()
        await backend.close()


async def test_ssh_into_a_microvm(keycard_server: tuple[int, Path]) -> None:
    """The whole product, end to end, over a hardware boundary."""
    port, key = keycard_server
    async with asyncssh.connect(
        "127.0.0.1", port=port, client_keys=[str(key)], known_hosts=None, username=ROOM
    ) as conn:
        async with conn.create_process(term_type="xterm-256color") as proc:
            proc.stdin.write("echo checked-in && exit\n")
            output = await asyncio.wait_for(proc.stdout.read(), timeout=90)

    assert "checked-in" in output


async def test_the_guest_is_a_different_kernel_from_the_host(
    keycard_server: tuple[int, Path],
) -> None:
    """The claim Phase 5 will make, reduced to something checkable.

    A container shares the host kernel, so `uname -r` inside one matches the
    host's. A microVM does not — it is booted from the kernel image keycard
    was configured with, and that is the isolation boundary in one line of
    output.
    """
    port, key = keycard_server
    async with asyncssh.connect(
        "127.0.0.1", port=port, client_keys=[str(key)], known_hosts=None, username=ROOM
    ) as conn:
        async with conn.create_process(term_type="xterm-256color") as proc:
            proc.stdin.write("uname -r && exit\n")
            guest = await asyncio.wait_for(proc.stdout.read(), timeout=90)

    assert os.uname().release not in guest


# -- helpers ----------------------------------------------------------------


async def _read_until(room: Room, needle: bytes, limit: float = 60.0) -> bytes:
    """Accumulate room output until *needle* shows up.

    Sleeping a fixed time instead would be the flakier version of this: a
    microVM's first prompt lands whenever the kernel and init are done, which
    is not a number anyone can pick in advance.
    """
    seen = b""

    async def pump() -> bytes:
        nonlocal seen
        while needle not in seen:
            chunk = await room.read()
            if not chunk:
                break
            seen += chunk
        return seen

    return await asyncio.wait_for(pump(), limit)
