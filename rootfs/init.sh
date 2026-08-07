#!/bin/sh
#
# keycard guest init — PID 1 inside a Firecracker room. Installed by
# build.sh as /usr/sbin/keycard-init, with /sbin/init pointed at it as a
# fallback for a kernel booted without an explicit init= on the cmdline.
#
# Deliberately a shell script rather than a real init. A keycard room runs
# exactly one process tree, rooted at one shell, for exactly one session
# (see FIRECRACKER.md) — there are no services to supervise, no runlevels,
# and no startup ordering to get right. The entire job is "make the
# pseudo-filesystems a pty and a login shell expect exist, then get out of
# the way", and systemd is several orders of magnitude of machinery more
# than that needs.
#
# The last line execs the agent, so the agent — not this script — is what
# ends up running as PID 1. If it ever exits, PID 1 is gone and the kernel
# panics; with panic=1 on the cmdline that halts the microVM, which is
# precisely the signal Phase 2's FirecrackerBackend wants (the room ended).
# Do not "fix" that by looping here.

warn() {
    echo "keycard-init: $*" >&2
}

# Best-effort and in dependency order. A failed mount is worth reporting on
# the serial console but is not worth refusing to boot over: a room missing
# /dev/shm is still a usable shell, and the operator needs that shell to
# work out why it was missing.
try_mount() {
    mount "$@" || warn "mount failed: $*"
}

try_mount -t proc  -o nosuid,nodev,noexec proc /proc
try_mount -t sysfs -o nosuid,nodev,noexec sys /sys

# Some kernels mount devtmpfs themselves (CONFIG_DEVTMPFS_MOUNT=y). Mounting
# it twice is harmless, but the warning would be noise, so check first.
[ -e /dev/null ] || try_mount -t devtmpfs -o nosuid,mode=0755 dev /dev

mkdir -p /dev/pts /dev/shm /run

# gid=5 is the tty group on Debian and Ubuntu, which covers every image
# keycard ships a room for. mode=0620 is what a normal login sets up, so the
# pty the agent allocates has the ownership a shell expects to find.
try_mount -t devpts -o nosuid,noexec,gid=5,mode=0620 devpts /dev/pts
try_mount -t tmpfs  -o nosuid,nodev                  tmpfs  /dev/shm
try_mount -t tmpfs  -o nosuid,nodev                  tmpfs  /tmp
try_mount -t tmpfs  -o nosuid,nodev,mode=0755        tmpfs  /run

# Not the `hostname` binary — the slim images do not all ship one, and this
# needs no binary at all.
echo keycard >/proc/sys/kernel/hostname 2>/dev/null || warn "could not set hostname"

exec /usr/bin/keycard-guest-agent --shell @KEYCARD_SHELL@
