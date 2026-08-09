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

Everything that can be tested without ``/dev/kvm`` lives in pure functions
here and is covered by ``tests/test_firecracker.py``; the rest is
``tests/test_firecracker_integration.py``, which boots real microVMs and
skips anywhere that can't. CI is one such place, so this module's real
coverage only ever runs on a developer's machine — see FIRECRACKER.md's
hardware prerequisite for what makes a host eligible.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import socket
import subprocess
import uuid
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import cast

from ..config import FirecrackerConfig, RoomConfig
from . import confine, vsock
from .base import Backend, Kept, Room
from .fcapi import SNAPSHOT_TIMEOUT, FirecrackerApi

log = logging.getLogger(__name__)

READ_SIZE = 65536

# Every directory this backend creates. A room's instance directory holds
# its disk image and, after `pause`, its RAM in the clear — none of which is
# any other local user's business.
OWNER_ONLY = 0o700

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
    always a number to supply here. This is also where the guest's RAM
    ceiling comes from, which is why ``confine.py`` does not chase cgroups:
    the guest cannot allocate past this number in the first place.
    ``pids_limit`` still has no meaning for a microVM and is still ignored —
    a guest has its own kernel and process table, so there is nothing
    host-side to cap. `config.py` warns at load when a room sets it, so that
    is a stated gap rather than a silent one.
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
    """The argv for the firecracker process.

    Deliberately still un-jailed. The jailer would replace this whole line —
    same API socket, wrapped in a chroot with cgroups — but it has to be run
    as root to do that, and a keycard that runs as root is the thing this
    backend is trying not to become. The confinement it would have provided
    is applied to the child instead; see `backends/confine.py`, and
    FIRECRACKER.md's Phase 4 for how that decision was reached.
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
        # 0700, because of what ends up in here: the room's whole disk image,
        # its console, and — once `pause` has run — a plaintext dump of its
        # RAM. The default runtime_dir is under /tmp, so inheriting the usual
        # 0755 would put all of that within reach of every local user.
        path.mkdir(parents=True, exist_ok=False, mode=OWNER_ONLY)
        instance = cls(id=instance_id, dir=path)
        instance.owner_pid.write_text(f"{os.getpid()}\n")
        return instance

    @property
    def owner_pid(self) -> Path:
        """The keycard process that created this instance.

        Written at creation, before anything else exists in here, so that a
        directory is attributable from the moment it appears — including
        during the seconds between `mkdir` and a VMM existing to record.
        """
        return self.dir / "owner.pid"

    @property
    def vmm_pid(self) -> Path:
        return self.dir / "vmm.pid"

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
    # No `--keep` on a microVM. Pausing one means snapshotting it, and a
    # Firecracker snapshot writes the whole of guest RAM to a file the VMM
    # later restores by reading directly — so it cannot be encrypted at rest
    # without still being plaintext at resume. Everything the guest touched
    # (keys, tokens, whatever was in a scrollback) would sit on disk in the
    # clear for the length of the window.
    #
    # That is a poor trade inside the one backend whose whole claim is that
    # guest escape is in scope, so the feature is refused rather than shipped
    # with a caveat. Rooms on this backend are destroyed on disconnect, which
    # is what every room did before `--keep` existed.
    #
    # Refusing is also the reversible direction: if the snapshot is ever made
    # genuinely secret, flipping this back to True adds a feature, while
    # taking it away later would break one.
    #
    # `pause`, `resume` and `destroy_kept` below are deliberately NOT deleted
    # along with the feature. A host upgraded from 0.2.x can still have
    # snapshots on disk that an older keycard wrote, and the startup sweep
    # added in 0.2.3 is what reaps them — that sweep, and the tests that
    # prove it works, both need a way to produce a paused room. Keeping the
    # machinery and refusing to reach for it is what lets old debris still
    # be cleaned up instead of stranded.
    can_pause = False

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


def _read_pid(pid_file: Path) -> int | None:
    try:
        return int(pid_file.read_text().strip())
    except (OSError, ValueError):
        return None


def _pid_is_alive(pid_file: Path) -> bool:
    """Whether *pid_file* names a process that still exists — nothing more.

    Deliberately does not check what the process *is*. This guards a live
    room, so the two ways of being wrong are not equal: mistaking a live
    keycard for a dead one deletes a running room's directory, while
    mistaking a recycled pid for the original only leaves debris a later
    sweep will catch. Insisting the command line say "keycard" would trade
    the safe error for the dangerous one every time the binary is invoked
    under another name — ``python -m keycard``, a wrapper, a supervisor.
    """
    pid = _read_pid(pid_file)
    return pid is not None and Path(f"/proc/{pid}").exists()


def _process_is(pid_file: Path, program: str) -> bool:
    """Whether *pid_file* names a live process running *program*.

    Reads ``/proc`` rather than sending signal 0, because a pid on its own is
    not evidence: pids are recycled. Here the stricter test is the safe one —
    it is only reached once the owning keycard is known to be gone, so a
    recycled number failing to match means the real VMM is dead and its
    directory really is debris.

    Compares the basename of ``argv[0]`` rather than searching the whole
    command line, which would match any process that merely *mentions* the
    program — a path, a config filename, an editor with the source open. The
    check is about what is running, not what it was pointed at.
    """
    pid = _read_pid(pid_file)
    if pid is None:
        return False
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return False
    argv0 = raw.split(b"\0", 1)[0]
    return bool(argv0) and PurePosixPath(argv0.decode(errors="replace")).name == program


def instance_is_debris(path: Path) -> bool:
    """Whether *path* is an instance directory with nothing left using it.

    Two owners have to be gone, not one. The keycard process that created it
    answers for a room still being built — there are seconds between `mkdir`
    and a VMM existing, and a concurrent keycard sweeping through that window
    must not mistake a half-built room for a dead one. The VMM answers for the
    other direction: if keycard was killed but its firecracker survived, the
    room is still running and removing the directory would pull the rootfs out
    from under a live guest.
    """
    if not path.is_dir():
        return False
    if _pid_is_alive(path / "owner.pid"):
        return False
    return not _process_is(path / "vmm.pid", "firecracker")


def reap_debris(runtime_dir: Path) -> list[str]:
    """Remove instance directories a crash left behind. Returns what went.

    `destroy` and `KeptRooms.destroy_all` between them clear up everything a
    *graceful* exit produces. Neither runs if keycard is SIGKILLed or the host
    loses power, and what survives is not inert: a paused room's directory
    holds a plaintext dump of the guest's RAM. SECURITY.md tells operators
    that dump lives only until the keep window expires and the room is
    reaped — which was true of every exit except the one an operator is least
    able to prevent.

    At startup rather than shutdown, and that is the whole reason it can
    exist. `close()` declines to do this on the way out because a directory
    it did not create belongs to someone else and removing it would be a
    guess. On the way in the question is answerable: each instance records
    who owns it, so debris can be identified rather than assumed.

    Note the two cases have opposite shapes, which is what makes this safe. A
    *paused* room has no VMM — `pause` stops the process once the snapshot is
    written — so the directories holding RAM dumps are exactly the ones with
    nothing running. A *running* room still has its VMM and has taken no
    snapshot yet, so the directories this refuses to touch are the ones with
    nothing sensitive at rest.
    """
    reaped: list[str] = []
    try:
        entries = sorted(runtime_dir.iterdir())
    except OSError:
        return reaped
    for path in entries:
        if not instance_is_debris(path):
            continue
        shutil.rmtree(path, ignore_errors=True)
        reaped.append(path.name)
    return reaped


class FirecrackerBackend(Backend):
    """Opens rooms as microVMs.

    Constructed lazily by ``routing.py`` — a Docker-only deployment never
    reaches this class, and so never has to have a kernel or a rootfs on
    disk. That is also why the preflight below can afford to be strict: by
    the time it runs, someone has explicitly asked for this backend.
    """

    # Must match `FirecrackerRoom.can_pause`, where the reasoning lives.
    can_pause = False

    def __init__(self, config: FirecrackerConfig) -> None:
        self._config = config
        self._binary = self._preflight()
        config.runtime_dir.mkdir(parents=True, exist_ok=True, mode=OWNER_ONLY)
        # `mkdir` leaves an existing directory's mode alone, and this one is
        # under /tmp by default — where a previous release created it 0755,
        # and where anyone can create it first.
        config.runtime_dir.chmod(OWNER_ONLY)
        # Before any room opens, so a crashed predecessor's plaintext RAM
        # dumps do not sit on disk for the lifetime of this process too.
        reaped = reap_debris(config.runtime_dir)
        if reaped:
            log.warning(
                "reaped %d instance director%s left by a previous run: %s",
                len(reaped),
                "y" if len(reaped) == 1 else "ies",
                ", ".join(reaped),
            )
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
        self._check_confinement()
        return binary

    def _check_confinement(self) -> None:
        """Decide once, at startup, whether the VMM will be confined.

        A kernel without Landlock is a property of the host, so finding out
        about it here — rather than per room — is both cheaper and the only
        way `required` can mean anything: refusing the first check-in of the
        day is not a security posture, it is an outage.
        """
        mode = self._config.confinement
        if mode == "off":
            log.warning("firecracker confinement is off; the VMM runs with keycard's own access")
            return
        abi = confine.abi_version()
        if abi:
            log.info("firecracker VMMs will be confined with Landlock (ABI %d)", abi)
            return
        if mode == "required":
            raise FirecrackerError(
                "[firecracker] confinement = 'required' but this kernel has no Landlock "
                "(needs Linux 5.13+ with CONFIG_SECURITY_LANDLOCK) — set it to "
                "'best-effort' to run anyway"
            )
        log.warning(
            "no Landlock on this kernel (needs Linux 5.13+); firecracker will run "
            "unconfined, with the same filesystem access as keycard itself"
        )

    def rootfs_for(self, room: RoomConfig) -> Path:
        """Where *room*'s built image lives.

        Defaults to ``<rootfs_dir>/<name>.ext4``, which is what
        ``rootfs/build.sh --room <name>`` writes, so the common case needs no
        per-room configuration at all.
        """
        if room.rootfs:
            return Path(room.rootfs).expanduser()
        return self._config.rootfs_dir / f"{room.name}.ext4"

    def _ruleset_for(self, instance: Instance) -> int | None:
        """The Landlock ruleset this microVM's VMM will be held to.

        None means unconfined, which `_preflight` has already decided is
        acceptable — either the mode is "off" or the kernel has no Landlock
        and the mode allows that. Everything the VMM legitimately touches is
        named here, so a path that turns out to be missing shows up as a
        boot failure rather than as a silent weakening.
        """
        if self._config.confinement == "off":
            return None
        assert self._config.kernel is not None  # noqa: S101 - _preflight guarantees this
        plan = confine.plan_for(
            binary=Path(self._binary),
            kernel=self._config.kernel,
            instance_dir=instance.dir,
        )
        return confine.build_ruleset(plan)

    def _fsize_for(self, instance: Instance, mem_mib: int) -> int:
        """A file-size ceiling above everything the VMM legitimately writes.

        Both floors matter: writes land at offsets up to the rootfs image's
        size, and a snapshot's memory file is exactly guest RAM. Taking only
        one of them would make either a big room or `pause` fail with
        SIGXFSZ, which is a miserable thing to debug.
        """
        rootfs_bytes = instance.rootfs.stat().st_size if instance.rootfs.exists() else 0
        return confine.fsize_limit(rootfs_bytes, mem_mib * 1024 * 1024)

    # -- process lifecycle ---------------------------------------------------

    async def _spawn(self, instance: Instance, fsize: int) -> _MicroVM:
        argv = firecracker_argv(self._binary, instance.api_sock)
        console = instance.console_log.open("ab")
        ruleset = self._ruleset_for(instance)
        try:
            process = await asyncio.create_subprocess_exec(
                *argv,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=console,
                stderr=asyncio.subprocess.STDOUT,
                # The ruleset has to survive the fd-closing pass to still be
                # there when `child_setup` restricts against it; the child
                # closes it itself once it has.
                pass_fds=() if ruleset is None else (ruleset,),
                preexec_fn=confine.child_setup(ruleset, fsize),  # noqa: PLW1509
            )
        finally:
            # The child holds its own dup of the fd; ours is just the handle
            # we used to hand it over.
            console.close()
            if ruleset is not None:
                os.close(ruleset)

        # Recorded before the readiness wait: if firecracker comes up and this
        # process dies during it, the startup sweep still has to be able to
        # tell that the VMM it left behind is alive.
        instance.vmm_pid.write_text(f"{process.pid}\n")

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
            vm = await self._spawn(
                instance, self._fsize_for(instance, machine_config(room)["mem_size_mib"])
            )
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
            # The memory file is already on disk at exactly guest RAM, which
            # is the same ceiling the next `pause` will need.
            mem_bytes = instance.mem_file.stat().st_size if instance.mem_file.exists() else 0
            vm = await self._spawn(instance, self._fsize_for(instance, mem_bytes // (1024 * 1024)))
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
        out — a concurrent keycard's live rooms look exactly like litter from
        here. `reap_debris` does it on the way *in* instead, where each
        directory's recorded owners make the question answerable.
        """
