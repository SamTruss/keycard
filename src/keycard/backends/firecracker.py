"""Firecracker microVM backend.

FIRECRACKER.md, Phase 2. Same three classes as the Docker backend, against a
hardware boundary instead of a namespace one: a room is a microVM booted from
the ext4 image ``rootfs/build.sh`` produced, and the pty lives inside it,
reached over vsock through the guest agent rather than over a Docker attach
socket.

The shape deliberately rhymes with ``docker.py``. Bytes move on the raw
socket via the loop's own socket methods, so there is no reader thread;
``destroy`` is idempotent because checkout races; ``pause`` releases this
connection's resources without ending the room. What is genuinely different
is that everything here is a process and a directory we own outright, so the
failure modes are ours to clean up: a half-configured microVM leaves a
running firecracker and a copied rootfs behind unless ``open`` unwinds
itself, which is what ``_cleanup`` below is for.

Nothing in this module has ever booted a microVM — see FIRECRACKER.md's
hardware prerequisite. Everything that can be tested without ``/dev/kvm``
lives in pure functions here and is covered by ``tests/test_firecracker.py``;
the rest waits on ``tests/test_firecracker_integration.py`` and a real host.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
import socket
import subprocess
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from ..config import FirecrackerConfig, RoomConfig
from . import vsock
from .base import Backend, Kept, Room
from .fcapi import SNAPSHOT_TIMEOUT, FirecrackerApi

log = logging.getLogger(__name__)

READ_SIZE = 65536

# The guest agent's defaults (guest-agent/src/main.rs). The host must connect
# to data first and control second: the agent accepts them in that order, so
# reversing it deadlocks both sides.
DATA_PORT = 10000
CTRL_PORT = 10001

# Firecracker reserves CIDs 0-2; 3 is the first a guest may use, and keycard
# only ever has one guest per firecracker process, so it is always 3.
GUEST_CID = 3

# Matches DockerBackend's ROOM_DEFAULTS mem_limit, so a room that configures
# nothing gets the same size whichever backend opens it.
DEFAULT_MEM_MIB = 1024
# Firecracker refuses a machine-config outside this range.
MIN_MEM_MIB = 16
MAX_VCPUS = 32
DEFAULT_VCPUS = 1

# How long the API socket may take to appear after exec. Unlike the boot
# wait, this is just process startup — if it takes longer than this,
# firecracker did not start.
API_READY_TIMEOUT = 5.0

# How long to wait for a "exit <code>" line after the data channel closes,
# and for the firecracker process to die after being asked politely.
EXIT_GRACE = 1.0
STOP_GRACE = 2.0

_MEMORY_UNITS = {"b": 1 / (1024 * 1024), "k": 1 / 1024, "m": 1.0, "g": 1024.0}


class FirecrackerError(RuntimeError):
    """Something went wrong that leaves no usable microVM."""


def parse_memory_mib(spec: str, default: int = DEFAULT_MEM_MIB) -> int:
    """Convert a Docker-style memory string to MiB.

    Accepts the same spellings ``mem_limit`` does — ``"512m"``, ``"1g"``, and
    a bare number meaning bytes — so a room's ``memory`` key means the same
    thing under either backend. An empty or unparseable value falls back to
    *default* rather than raising: the Docker backend treats an unset
    ``memory`` as "keep the blanket default", and a room shouldn't fail to
    open on one backend over a string the other one shrugged at.
    """
    spec = spec.strip().lower()
    if not spec:
        return default
    unit = _MEMORY_UNITS.get(spec[-1:], None)
    number = spec[:-1] if unit is not None else spec
    try:
        value = float(number) * (unit if unit is not None else _MEMORY_UNITS["b"])
    except ValueError:
        log.warning("could not parse memory %r; using %d MiB", spec, default)
        return default
    return max(MIN_MEM_MIB, int(value))


def machine_config(room: RoomConfig) -> dict[str, int]:
    """The ``PUT /machine-config`` body for *room*.

    Both keys are required by the API, so unlike the Docker backend — where
    an unset ``memory``/``cpus`` simply leaves a default alone — there is
    always a number to supply here. Full resource parity (cgroups via the
    jailer, and what ``pids_limit`` should even mean for a microVM) is
    Phase 4; this is the part the VM cannot boot without.
    """
    return {
        "vcpu_count": max(1, min(room.cpus or DEFAULT_VCPUS, MAX_VCPUS)),
        "mem_size_mib": parse_memory_mib(room.memory),
    }


def parse_exit(line: bytes) -> int | None:
    """Read the guest agent's ``exit <code>`` control message.

    Returns None for anything else on the control channel, so an unknown verb
    is ignored rather than mistaken for a status.
    """
    parts = line.split()
    if len(parts) != 2 or parts[0] != b"exit":
        return None
    try:
        return int(parts[1])
    except ValueError:
        return None


def kvm_present() -> bool:
    """Whether this host can run a microVM at all.

    Its own function so the rest of the backend can be exercised on a machine
    without it — which, until FIRECRACKER.md's hardware prerequisite is met,
    is every machine this project has.
    """
    return Path("/dev/kvm").exists()


def firecracker_argv(binary: str, api_sock: Path) -> list[str]:
    """The argv for a raw (un-jailed) firecracker process.

    Its own function because it is exactly what the jailer replaces: same API
    socket, but wrapped in a chroot with its own cgroup and seccomp setup.
    Running raw first is deliberate (see FIRECRACKER.md's open questions) —
    the VM boundary is KVM either way, and writing the jailer's setup before
    anything has ever booted means debugging two unverified layers at once.
    Adding it later should mean a second function beside this one, not
    surgery on `open`.
    """
    return [binary, "--api-sock", str(api_sock)]


def _copy_rootfs(src: Path, dst: Path) -> None:
    """Give a room its own copy of the shared rootfs image.

    Copy-on-write where the filesystem can do it: a room writes to its root
    filesystem constantly, so it cannot share the built image, but a
    reflink makes "cannot share" cost nothing on btrfs or XFS. ``--reflink=auto``
    already degrades to a full copy on filesystems without it; the shutil
    fallback is for hosts with no ``cp`` at all.
    """
    try:
        subprocess.run(  # noqa: S603 - fixed argv, no shell, paths are ours
            ["/bin/cp", "--reflink=auto", str(src), str(dst)],
            check=True,
            capture_output=True,
        )
        return
    except (OSError, subprocess.CalledProcessError) as exc:
        log.debug("cp --reflink unavailable (%s); falling back to a full copy", exc)
    shutil.copyfile(src, dst)


@dataclass(frozen=True)
class Instance:
    """One microVM's directory on the host.

    Everything a room owns lives under a single directory so that destroying
    it — including a snapshot taken by `pause` — is one ``rmtree``. That is
    the answer to FIRECRACKER.md's snapshot-lifecycle question: there is no
    separate snapshot store to reap, because the snapshot is inside the thing
    that already had to be reaped.
    """

    id: str
    dir: Path

    @classmethod
    def create(cls, runtime_dir: Path, room_name: str) -> Instance:
        # Room name in the id purely so `ls /tmp/keycard` is readable when
        # something has leaked; uniqueness comes from the uuid.
        instance_id = f"{room_name}-{uuid.uuid4().hex[:12]}"
        path = runtime_dir / instance_id
        path.mkdir(parents=True, exist_ok=False)
        return cls(id=instance_id, dir=path)

    @property
    def api_sock(self) -> Path:
        return self.dir / "api.sock"

    @property
    def vsock_sock(self) -> Path:
        return self.dir / "vsock.sock"

    @property
    def rootfs(self) -> Path:
        return self.dir / "rootfs.ext4"

    @property
    def snapshot(self) -> Path:
        return self.dir / "snapshot"

    @property
    def mem_file(self) -> Path:
        return self.dir / "memory"

    @property
    def console_log(self) -> Path:
        """Where the guest's serial console and firecracker's own complaints
        land. The boot args put the kernel on ttyS0, which firecracker writes
        to its stdout — so when a microVM boots to nothing, this file is the
        only thing that will say why."""
        return self.dir / "console.log"


@dataclass
class _MicroVM:
    """A live firecracker process and the instance it is running."""

    instance: Instance
    api: FirecrackerApi
    process: asyncio.subprocess.Process


class FirecrackerKept(Kept):
    """A room paused to a snapshot on disk.

    Holds the instance rather than a snapshot path: restoring needs the
    rootfs copy and the vsock socket path back at their original locations
    too, and destroying needs all of it gone.
    """

    def __init__(self, instance: Instance) -> None:
        self.instance = instance


class FirecrackerRoom(Room):
    def __init__(self, vm: _MicroVM, data: socket.socket, ctrl: socket.socket) -> None:
        self._vm = vm
        self._data = data
        self._ctrl = ctrl
        self._loop = asyncio.get_running_loop()
        self._destroyed = False
        self._lock = asyncio.Lock()
        self._exit_status: int | None = None
        # The control channel is the only place a shell's exit code ever
        # appears — the microVM keeps running after the shell exits, so
        # unlike a container there is no process status to read afterwards.
        self._exit_watch = asyncio.create_task(self._watch_exit())

    async def read(self) -> bytes:
        try:
            return await self._loop.sock_recv(self._data, READ_SIZE)
        except (OSError, asyncio.CancelledError):
            return b""

    async def write(self, data: bytes) -> None:
        try:
            await self._loop.sock_sendall(self._data, data)
        except OSError:
            log.debug("write to room failed; it has probably gone")

    async def resize(self, width: int, height: int) -> None:
        try:
            await self._loop.sock_sendall(self._ctrl, f"resize {width} {height}\n".encode())
        except OSError:
            log.debug("resize failed; room may have ended")

    async def _watch_exit(self) -> None:
        """Collect the shell's exit status off the control channel."""
        buffer = b""
        while True:
            try:
                chunk = await self._loop.sock_recv(self._ctrl, READ_SIZE)
            except (OSError, asyncio.CancelledError):
                return
            if not chunk:
                return
            buffer += chunk
            while b"\n" in buffer:
                line, buffer = buffer.split(b"\n", 1)
                status = parse_exit(line)
                if status is not None:
                    self._exit_status = status
                    return

    def _close_attachment(self) -> None:
        self._exit_watch.cancel()
        for sock in (self._data, self._ctrl):
            try:
                sock.close()
            except OSError:
                log.debug("socket already closed")

    async def destroy(self) -> int:
        async with self._lock:
            if self._destroyed:
                return 0
            self._destroyed = True

        # The data channel closing and the exit line arriving are two
        # different events on two different sockets, and destroy() is usually
        # called the instant the first one happens. Give the second a moment
        # before deciding we never heard it.
        try:
            await asyncio.wait_for(asyncio.shield(self._exit_watch), EXIT_GRACE)
        except (TimeoutError, asyncio.CancelledError):
            log.debug("no exit status from the guest agent")

        self._close_attachment()
        await _stop(self._vm)
        await _remove_dir(self._vm.instance.dir)
        status = self._exit_status if self._exit_status is not None else 0
        log.info("room destroyed: %s (status %s)", self._vm.instance.id, status)
        return status

    async def pause(self) -> FirecrackerKept:
        """Snapshot the microVM to disk and stop the process.

        Unlike Docker's pause — which parks a container in the cgroup freezer
        and keeps its memory resident — this writes guest RAM out and frees
        it, because a microVM's memory is a real host allocation with nothing
        else to reclaim it. The cost lands on disk instead, which is why the
        keep window has to actually reap: see `destroy_kept`.
        """
        self._close_attachment()
        instance = self._vm.instance
        await self._vm.api.patch("/vm", {"state": "Paused"})
        await self._vm.api.put(
            "/snapshot/create",
            {
                "snapshot_type": "Full",
                "snapshot_path": str(instance.snapshot),
                "mem_file_path": str(instance.mem_file),
            },
            timeout=SNAPSHOT_TIMEOUT,
        )
        await _stop(self._vm)
        log.info("room snapshotted: %s", instance.id)
        return FirecrackerKept(instance)


async def _stop(vm: _MicroVM) -> None:
    """End the firecracker process.

    There is no graceful shell exit to wait for here the way there is with a
    container: by the time anything calls this the guest is either finished
    or being cut off, and either way the microVM is a power cut. SIGTERM
    first anyway, because firecracker handles it and releases /dev/kvm and
    its sockets on the way out.
    """
    if vm.process.returncode is not None:
        return
    try:
        vm.process.terminate()
    except ProcessLookupError:
        return
    try:
        await asyncio.wait_for(vm.process.wait(), STOP_GRACE)
        return
    except TimeoutError:
        log.debug("firecracker %s ignored SIGTERM; killing", vm.instance.id)
    try:
        vm.process.kill()
        await vm.process.wait()
    except ProcessLookupError:
        pass


async def _remove_dir(path: Path) -> None:
    await asyncio.get_running_loop().run_in_executor(
        None, lambda: shutil.rmtree(path, ignore_errors=True)
    )


class FirecrackerBackend(Backend):
    """Opens rooms as microVMs.

    Constructed lazily by ``routing.py`` — a Docker-only deployment never
    reaches this class, and so never has to have a kernel or a rootfs on
    disk. That is also why the preflight below can afford to be strict: by
    the time it runs, someone has explicitly asked for this backend.
    """

    def __init__(self, config: FirecrackerConfig) -> None:
        self._config = config
        self._binary = self._preflight()
        config.runtime_dir.mkdir(parents=True, exist_ok=True)
        log.info(
            "firecracker backend ready: kernel=%s rootfs_dir=%s runtime_dir=%s",
            config.kernel,
            config.rootfs_dir,
            config.runtime_dir,
        )

    def _preflight(self) -> str:
        """Fail at startup, with the thing to fix, rather than on the first
        connection — a missing kernel is a deployment mistake, and finding
        out about it when a user tries to check in is too late."""
        cfg = self._config
        binary = shutil.which(cfg.binary)
        if binary is None:
            raise FirecrackerError(
                f"firecracker binary {cfg.binary!r} not found on PATH — "
                "install it, or set [firecracker] binary = '/path/to/firecracker'"
            )
        if cfg.kernel is None:
            raise FirecrackerError(
                "the firecracker backend needs a guest kernel — "
                "set [firecracker] kernel = '/path/to/vmlinux'"
            )
        if not cfg.kernel.is_file():
            raise FirecrackerError(f"no guest kernel at {cfg.kernel}")
        if not cfg.rootfs_dir.is_dir():
            raise FirecrackerError(
                f"no rootfs directory at {cfg.rootfs_dir} — "
                "build one with `sudo rootfs/build.sh --room ubuntu`"
            )
        if not kvm_present():
            raise FirecrackerError(
                "/dev/kvm is not present — the firecracker backend needs hardware "
                "virtualization (bare metal, or a VM with nested virt enabled)"
            )
        return binary

    def rootfs_for(self, room: RoomConfig) -> Path:
        """Where *room*'s built image lives.

        Defaults to ``<rootfs_dir>/<name>.ext4``, which is what
        ``rootfs/build.sh --room <name>`` writes, so the common case needs no
        per-room configuration at all.
        """
        if room.rootfs:
            return Path(room.rootfs).expanduser()
        return self._config.rootfs_dir / f"{room.name}.ext4"

    # -- process lifecycle ---------------------------------------------------

    async def _spawn(self, instance: Instance) -> _MicroVM:
        argv = firecracker_argv(self._binary, instance.api_sock)
        console = instance.console_log.open("ab")
        try:
            process = await asyncio.create_subprocess_exec(
                *argv,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=console,
                stderr=asyncio.subprocess.STDOUT,
            )
        finally:
            # The child holds its own dup of the fd; ours is just the handle
            # we used to hand it over.
            console.close()

        vm = _MicroVM(instance, FirecrackerApi(instance.api_sock), process)
        try:
            await vm.api.wait_ready(API_READY_TIMEOUT)
        except TimeoutError as exc:
            await _stop(vm)
            raise FirecrackerError(
                f"firecracker did not come up for {instance.id} — see {instance.console_log}"
            ) from exc
        return vm

    async def _attach(self, vm: _MicroVM, width: int, height: int) -> FirecrackerRoom:
        """Connect both guest channels and set the initial terminal size."""
        uds = vm.instance.vsock_sock
        timeout = self._config.boot_timeout_seconds
        # Data first, then control — the agent accepts in that order.
        data = await vsock.connect_with_retry(uds, DATA_PORT, timeout)
        try:
            # The agent is already past its accept for data by now, so the
            # control connection is not waiting on a boot and does not need
            # the retry budget a second time.
            ctrl = await vsock.connect(uds, CTRL_PORT)
        except vsock.VsockError:
            data.close()
            raise

        room = FirecrackerRoom(vm, data, ctrl)
        # The agent's first control line doubles as the initial size, so this
        # is not merely an optimisation — without it the guest pty stays at
        # its 80x24 fallback until the user resizes their terminal.
        await room.resize(width, height)
        return room

    async def _cleanup(self, vm: _MicroVM | None, instance: Instance) -> None:
        """Unwind a half-built room. Nothing above this layer knows the
        instance exists yet, so if `open` raises, this is the only thing that
        will ever remove it."""
        if vm is not None:
            await _stop(vm)
        await _remove_dir(instance.dir)

    # -- Backend -------------------------------------------------------------

    async def open(self, room: RoomConfig, width: int, height: int) -> Room:
        image = self.rootfs_for(room)
        if not image.is_file():
            raise FirecrackerError(
                f"no rootfs for room {room.name!r} at {image} — "
                f"build one with `sudo rootfs/build.sh --room {room.name}`"
            )

        loop = asyncio.get_running_loop()
        instance = await loop.run_in_executor(
            None, Instance.create, self._config.runtime_dir, room.name
        )
        vm: _MicroVM | None = None
        try:
            await loop.run_in_executor(None, _copy_rootfs, image, instance.rootfs)
            vm = await self._spawn(instance)
            await self._configure(vm, room)
            await vm.api.put("/actions", {"action_type": "InstanceStart"})
            attached = await self._attach(vm, width, height)
        except BaseException:
            await self._cleanup(vm, instance)
            raise

        log.info("room opened: %s (%s)", instance.id, image.name)
        return attached

    async def _configure(self, vm: _MicroVM, room: RoomConfig) -> None:
        """Every API call a cold boot needs, in the order Firecracker wants."""
        instance = vm.instance
        kernel = self._config.kernel
        assert kernel is not None  # noqa: S101 - _preflight guarantees this
        await vm.api.put(
            "/boot-source",
            {"kernel_image_path": str(kernel), "boot_args": self._config.boot_args},
        )
        await vm.api.put(
            "/drives/rootfs",
            {
                "drive_id": "rootfs",
                "path_on_host": str(instance.rootfs),
                "is_root_device": True,
                "is_read_only": False,
            },
        )
        await vm.api.put("/machine-config", machine_config(room))
        # No network device at all, which is the microVM equivalent of the
        # Docker backend's `network = "none"`. Phase 3 adds tap devices; until
        # then a firecracker room is offline, and that is the safer default to
        # be stuck on.
        await vm.api.put("/vsock", {"guest_cid": GUEST_CID, "uds_path": str(instance.vsock_sock)})

    async def resume(self, kept: Kept, width: int, height: int) -> Room:
        instance = cast(FirecrackerKept, kept).instance
        # A stale socket from the process that took the snapshot would make
        # the new one fail to bind.
        instance.api_sock.unlink(missing_ok=True)
        instance.vsock_sock.unlink(missing_ok=True)

        vm: _MicroVM | None = None
        try:
            vm = await self._spawn(instance)
            # Must be the first call on this socket: loading a snapshot
            # restores the machine config, drives and vsock device wholesale,
            # and Firecracker rejects it once anything else has been set.
            await vm.api.put(
                "/snapshot/load",
                {
                    "snapshot_path": str(instance.snapshot),
                    "mem_backend": {
                        "backend_path": str(instance.mem_file),
                        "backend_type": "File",
                    },
                    "enable_diff_snapshots": False,
                    "resume_vm": True,
                },
                timeout=SNAPSHOT_TIMEOUT,
            )
            attached = await self._attach(vm, width, height)
        except BaseException:
            # Unlike open(), do not remove the instance directory: the caller
            # still holds the Kept, and destroy_kept is what owns removing it.
            if vm is not None:
                await _stop(vm)
            raise

        log.info("room resumed: %s", instance.id)
        return attached

    async def destroy_kept(self, kept: Kept) -> None:
        instance = cast(FirecrackerKept, kept).instance
        await _remove_dir(instance.dir)
        log.info("kept room destroyed: %s", instance.id)

    async def close(self) -> None:
        """Nothing backend-level to release.

        Every resource this backend owns belongs to an instance directory,
        and rooms and kept rooms are torn down through `destroy` and
        `destroy_kept` before shutdown gets here. Anything left in
        ``runtime_dir`` is the debris of a crash, and deleting directories
        this process may not have created is not a thing to do on the way
        out.
        """
