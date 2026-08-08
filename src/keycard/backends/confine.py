"""Unprivileged confinement for the firecracker process.

FIRECRACKER.md, Phase 4. The upstream answer is the jailer, and the jailer
needs root: it unshares a mount namespace, chroots, mknods its own
``/dev/kvm``, and drops to a uid you name. keycard is not root and is
trying hard not to become root — see Phase 3 on why — so the jailer's exact
shape is unavailable to it.

Most of what the jailer buys is available anyway, because its model is "you
are root, let me drop you safely" and keycard already starts where that
ends:

===============  ==========================================================
seccomp          already on; firecracker installs its own filters by
                 default and ``--no-seccomp`` is the opt-out
unprivileged     already true; the VMM inherits keycard's user
chroot           replaced by Landlock, below
rlimits          ``setrlimit`` in the child, no privilege required
cgroups          deliberately omitted — see the note at the end
===============  ==========================================================

What is left is the part that matters most. Without confinement a
compromised VMM runs as the keycard user, which means it can read every
other room's rootfs image, every other instance directory, and
``~/.config/keycard/host_key``. Landlock (Linux 5.13+) is an unprivileged
LSM that lets a process irrevocably drop its own filesystem access, and the
restriction is inherited across ``execve`` — exactly the shape needed to
hand a restricted world to a process we are about to exec.

The allowlist is small because firecracker is a statically linked binary:
no loader, no shared libraries, no ``ld.so.cache``. It needs itself, the
guest kernel, ``/dev/kvm``, and its own instance directory. Everything else
on the filesystem becomes unreachable, including the paths above.

**Cgroups are deliberately not here.** Reaching them without privilege
means ``systemd-run --user --scope``, which needs a user D-Bus session that
a system service does not have, and wrapping the VMM in a supervisor
process would complicate every signal path in ``_stop``. Guest RAM is
already capped by ``mem_size_mib``, so what is left uncovered is VMM
overhead rather than anything the guest can allocate directly. If this
becomes worth doing, it belongs behind an explicit config key rather than
as a silent default that works on laptops and not on servers.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import logging
import os
import resource
import struct
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

# Landlock's three syscalls. The numbers are the same on every architecture
# that has them, because they were allocated after the generic table existed.
_SYS_CREATE_RULESET = 444
_SYS_ADD_RULE = 445
_SYS_RESTRICT_SELF = 446

# Passed as `flags` to landlock_create_ruleset to ask "which ABI is this?"
# instead of actually creating one.
_CREATE_RULESET_VERSION = 1 << 0
_RULE_PATH_BENEATH = 1

_PR_SET_NO_NEW_PRIVS = 38

# LANDLOCK_ACCESS_FS_*, in the order the uapi header declares them.
_EXECUTE = 1 << 0
_WRITE_FILE = 1 << 1
_READ_FILE = 1 << 2
_READ_DIR = 1 << 3
_REMOVE_DIR = 1 << 4
_REMOVE_FILE = 1 << 5
_MAKE_CHAR = 1 << 6
_MAKE_DIR = 1 << 7
_MAKE_REG = 1 << 8
_MAKE_SOCK = 1 << 9
_MAKE_FIFO = 1 << 10
_MAKE_BLOCK = 1 << 11
_MAKE_SYM = 1 << 12
_REFER = 1 << 13
_TRUNCATE = 1 << 14

# Everything each ABI version knows about. Handing landlock_create_ruleset a
# bit its kernel has never heard of is EINVAL, so this is a ceiling rather
# than a wish list — and a newer kernel than we know about is capped at the
# newest set we do know, which costs nothing but the bits we never use.
_HANDLED_BY_ABI = {
    1: (1 << 13) - 1,
    2: (1 << 14) - 1,
    3: (1 << 15) - 1,
}
_NEWEST_ABI = max(_HANDLED_BY_ABI)

# The three shapes of access this backend actually hands out.
READ = _READ_FILE | _READ_DIR
EXECUTE = _EXECUTE | _READ_FILE
WRITE = (
    READ
    | _WRITE_FILE
    | _TRUNCATE
    | _REMOVE_DIR
    | _REMOVE_FILE
    | _MAKE_DIR
    | _MAKE_REG
    | _MAKE_SOCK
    | _MAKE_FIFO
    | _MAKE_SYM
)

# The rights that mean anything for something that is not a directory.
# Landlock rejects a rule outright — EINVAL, not a quiet no-op — if it names
# a regular file or a device and asks for `READ_DIR` or any of the `MAKE_*`
# rights, so access has to be narrowed to the kind of thing it lands on.
FILE_ACCESS = _EXECUTE | _READ_FILE | _WRITE_FILE | _TRUNCATE

# The device that makes a microVM a microVM. Read-write: firecracker opens
# it and drives it with ioctls, which Landlock does not filter at this ABI.
KVM = Path("/dev/kvm")

# Enough file descriptors for the VMM's own sockets, the vsock connections,
# and the guest's block device, with a wide margin — this exists to bound a
# runaway, not to be a tight budget.
NOFILE = 1024

# `fsize` has to clear the largest file firecracker legitimately writes. The
# memory snapshot is exactly guest RAM, and writes into the rootfs image
# land at offsets up to its size, so both are floors rather than one.
FSIZE_HEADROOM = 256 * 1024 * 1024


class ConfinementError(Exception):
    """Confinement was required and could not be applied."""


# The two uapi structs, as bytes rather than `ctypes.Structure`. Both are
# small and one of them is `__packed__` — "=" gives native byte order at
# standard size with no alignment padding, which is exactly what packed
# means, and avoids `_pack_`, whose implicit layout ctypes now deprecates.
_RULESET_ATTR = "=Q"  # handled_access_fs
_PATH_BENEATH_ATTR = "=Qi"  # allowed_access, parent_fd


def _load_libc() -> ctypes.CDLL | None:
    """None on anything that isn't a Linux with a findable libc.

    Confinement is a Linux-only concern and this module is only reached
    through the Firecracker backend, but import-time is the wrong place to
    discover that: `abi_version()` returning 0 lets every caller take the
    same "no Landlock here" path it already has to handle.
    """
    try:
        libc = ctypes.CDLL(ctypes.util.find_library("c") or "libc.so.6", use_errno=True)
    except OSError:  # pragma: no cover - needs a host with no loadable libc
        return None
    libc.syscall.restype = ctypes.c_long
    return libc


_libc = _load_libc()


def _syscall(number: int, *args: object) -> int:
    if _libc is None:  # pragma: no cover - guarded by abi_version() upstream
        raise ConfinementError("no libc to make syscalls through")
    ctypes.set_errno(0)
    return int(_libc.syscall(ctypes.c_long(number), *args))


def abi_version() -> int:
    """The kernel's Landlock ABI version, or 0 if it has none.

    0 covers every way this can be unavailable — kernel too old, Landlock
    compiled out, or a seccomp filter between us and the syscall — because
    the caller's response to all of them is identical.
    """
    if _libc is None:
        return 0
    result = _syscall(
        _SYS_CREATE_RULESET, None, ctypes.c_size_t(0), ctypes.c_uint32(_CREATE_RULESET_VERSION)
    )
    return result if result > 0 else 0


@dataclass(frozen=True)
class Plan:
    """Everything the VMM may reach, and by implication everything it may not.

    Split by access rather than given per-path flags because the split is
    the interesting part: exactly one directory is writable, and it is the
    one belonging to this room.
    """

    executable: tuple[Path, ...] = ()
    readable: tuple[Path, ...] = ()
    writable: tuple[Path, ...] = ()

    def rules(self) -> Iterator[tuple[Path, int]]:
        for path in self.executable:
            yield path, EXECUTE
        for path in self.readable:
            yield path, READ
        for path in self.writable:
            yield path, WRITE


def plan_for(
    binary: Path,
    kernel: Path,
    instance_dir: Path,
    extra_readable: Iterable[Path] = (),
) -> Plan:
    """What one microVM needs, and nothing more.

    `instance_dir` is the only writable entry, which is the whole point:
    every other room's directory is a sibling of it, and a sibling is not
    beneath it. `extra_readable` exists for hosts that turn out to need
    something this list does not predict — it is config, not a guess.
    """
    return Plan(
        executable=(binary,),
        readable=(kernel, *extra_readable),
        writable=(instance_dir, KVM),
    )


def build_ruleset(plan: Plan) -> int | None:
    """Create a Landlock ruleset for *plan* and return its file descriptor.

    None means the kernel has no Landlock. Raises `ConfinementError` if it
    has Landlock and we still could not build the ruleset — that is a bug or
    a missing path, not a host capability question, and quietly running
    unconfined would be the wrong answer to it.
    """
    abi = abi_version()
    if abi == 0:
        return None
    handled = _HANDLED_BY_ABI[min(abi, _NEWEST_ABI)]

    attr = struct.pack(_RULESET_ATTR, handled)
    fd = _syscall(
        _SYS_CREATE_RULESET,
        attr,
        ctypes.c_size_t(len(attr)),
        ctypes.c_uint32(0),
    )
    if fd < 0:
        raise ConfinementError(f"landlock_create_ruleset failed: {os.strerror(ctypes.get_errno())}")

    try:
        for path, access in plan.rules():
            _add_rule(fd, path, access & handled)
    except BaseException:
        os.close(fd)
        raise
    return fd


def _add_rule(ruleset_fd: int, path: Path, access: int) -> None:
    try:
        # O_PATH: we want to name the file for Landlock, not read it. It also
        # means this works for /dev/kvm without opening the device.
        path_fd = os.open(path, os.O_PATH | os.O_CLOEXEC)
    except OSError as exc:
        raise ConfinementError(f"cannot confine to {path}: {exc}") from exc
    try:
        if not os.path.isdir(path):
            access &= FILE_ACCESS
        rule = struct.pack(_PATH_BENEATH_ATTR, access, path_fd)
        result = _syscall(
            _SYS_ADD_RULE,
            ctypes.c_int(ruleset_fd),
            ctypes.c_int(_RULE_PATH_BENEATH),
            rule,
            ctypes.c_uint32(0),
        )
        if result != 0:
            raise ConfinementError(
                f"landlock_add_rule for {path} failed: {os.strerror(ctypes.get_errno())}"
            )
    finally:
        os.close(path_fd)


def fsize_limit(*sizes: int) -> int:
    """A file-size ceiling that clears everything firecracker writes."""
    return max(sizes, default=0) + FSIZE_HEADROOM


def child_setup(ruleset_fd: int | None, fsize: int, nofile: int = NOFILE) -> Callable[[], None]:
    """Build the `preexec_fn` that confines the child.

    Everything expensive — the ruleset, the ctypes machinery, the argument
    objects — is already built by the time this returns, because the callable
    runs between fork and exec, where allocation in a process that has
    threads is how you find a deadlock. What is left in the child is two raw
    syscalls, two setrlimits and a close.
    """

    libc = _libc
    if ruleset_fd is not None and libc is None:  # pragma: no cover - unreachable via build_ruleset
        raise ConfinementError("have a ruleset but no libc to apply it with")
    # Resolved here rather than in the child: an attribute lookup is an
    # allocation, and the child is running between fork and exec.
    number = ctypes.c_long(_SYS_RESTRICT_SELF)
    fd_arg = ctypes.c_int(ruleset_fd or 0)
    flags = ctypes.c_uint32(0)

    def setup() -> None:
        resource.setrlimit(resource.RLIMIT_FSIZE, (fsize, fsize))
        resource.setrlimit(resource.RLIMIT_NOFILE, (nofile, nofile))
        if ruleset_fd is None or libc is None:
            return
        # Landlock refuses to restrict a process that could still gain
        # privilege through a setuid exec, so this is a precondition rather
        # than extra hardening.
        ctypes.set_errno(0)
        if libc.prctl(_PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) != 0:
            raise OSError(ctypes.get_errno(), "PR_SET_NO_NEW_PRIVS failed")
        ctypes.set_errno(0)
        if libc.syscall(number, fd_arg, flags) != 0:
            raise OSError(ctypes.get_errno(), "landlock_restrict_self failed")
        os.close(ruleset_fd)

    return setup
