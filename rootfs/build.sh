#!/usr/bin/env bash
#
# Build a Firecracker rootfs from a keycard room's Docker image.
#
# FIRECRACKER.md, Phase 1. The room definitions in keycard.toml stay the
# source of truth for what a room *is*; this derives an ext4 image from one
# of them rather than introducing a second place where "the python room" is
# defined. That is why --room resolves through keycard.config instead of
# carrying its own name-to-image map.
#
# What comes out is a flat ext4 filesystem holding the image's userland, the
# Phase 0 guest agent at /usr/bin/keycard-guest-agent, and a small init at
# /usr/sbin/keycard-init for PID 1 to exec (see init.sh). It is not bootable
# on its own — Phase 2 supplies the kernel and the cmdline. See README.md.
#
# Needs root: unpacking a container filesystem preserves ownership and
# device nodes, which an unprivileged user cannot recreate. It does not need
# a loop device or /dev/kvm — mke2fs -d builds a filesystem straight from a
# directory tree — so this runs anywhere, CI included.

set -euo pipefail

unset CDPATH
SCRIPT_DIR=$(cd -- "$(dirname -- "$0")" && pwd)
REPO_ROOT=$(dirname "$SCRIPT_DIR")
AGENT_DIR="$REPO_ROOT/guest-agent"

# Static by default. The agent is built on the host but runs against the
# *image's* libc, and those are not the same vintage — a glibc build on a
# newer host silently produces a binary the guest cannot start. musl links
# it all in, which removes the coupling entirely.
AGENT_TARGET="x86_64-unknown-linux-musl"

ROOM="ubuntu"
IMAGE=""
FROM_TAR=""
OUT=""
SIZE_MB=""
AGENT_BIN=""
SHELL_PATH=""
KEEP_STAGING=0

# Matches DockerBackend's ROOM_DEFAULTS command, so a room's shell does not
# depend on which backend opened it.
DEFAULT_SHELL="/bin/bash"
FALLBACK_SHELL="/bin/sh"

usage() {
    cat >&2 <<'EOF'
usage: sudo rootfs/build.sh [options]

  --room NAME     keycard room to build (default: ubuntu); its image is
                  resolved through keycard's own config loader
  --image IMAGE   Docker image to use directly, skipping room lookup
  --from-tar PATH pre-flattened rootfs tar instead of a Docker image; the
                  escape hatch for hosts with no Docker daemon
  --out PATH      output ext4 image (default: rootfs/build/<room>.ext4)
  --size MB       filesystem size; default is content plus 30% and 64 MiB
  --agent PATH    prebuilt guest agent; default is to cargo build it
  --shell PATH    shell the agent execs in the guest (default: /bin/bash,
                  falling back to /bin/sh if the image has no bash)
  --keep-staging  leave the unpacked tree on disk for inspection
EOF
}

die() {
    echo "build.sh: $*" >&2
    exit 1
}

log() {
    echo "==> $*" >&2
}

while [ $# -gt 0 ]; do
    case "$1" in
        --room)         ROOM=${2:?--room needs a value}; shift 2 ;;
        --image)        IMAGE=${2:?--image needs a value}; shift 2 ;;
        --from-tar)     FROM_TAR=${2:?--from-tar needs a value}; shift 2 ;;
        --out)          OUT=${2:?--out needs a value}; shift 2 ;;
        --size)         SIZE_MB=${2:?--size needs a value}; shift 2 ;;
        --agent)        AGENT_BIN=${2:?--agent needs a value}; shift 2 ;;
        --shell)        SHELL_PATH=${2:?--shell needs a value}; shift 2 ;;
        --keep-staging) KEEP_STAGING=1; shift ;;
        -h|--help)      usage; exit 0 ;;
        *)              usage; die "unrecognized argument $1" ;;
    esac
done

# An explicit --shell is a promise the caller made about the image, so a
# missing one is an error later; the default is only a preference, and falls
# back quietly.
SHELL_EXPLICIT=1
if [ -z "$SHELL_PATH" ]; then
    SHELL_PATH=$DEFAULT_SHELL
    SHELL_EXPLICIT=0
fi

# ---------------------------------------------------------------- preflight

[ "$(id -u)" -eq 0 ] || die "must run as root (see the header comment for why)"
[ "$(uname -s)" = "Linux" ] || die "Linux only — this builds a Linux filesystem"

need() {
    command -v "$1" >/dev/null 2>&1 || die "$1 not found; $2"
}

need mke2fs "install e2fsprogs"
need debugfs "install e2fsprogs"
need tar "install tar"

# mke2fs -d landed in e2fsprogs 1.43. Older ones would need a loop mount,
# which is the whole thing this script avoids, so fail loudly instead of
# quietly building an empty filesystem.
mke2fs_version=$(mke2fs -V 2>&1 | head -1 | awk '{print $2}')
case "$mke2fs_version" in
    1.4[0-2].*|1.[0-3][0-9].*) die "e2fsprogs $mke2fs_version is too old; -d needs 1.43+" ;;
esac

if [ -n "$FROM_TAR" ]; then
    [ -f "$FROM_TAR" ] || die "no such tar: $FROM_TAR"
    [ -z "$IMAGE" ] || die "--from-tar and --image are mutually exclusive"
else
    need docker "install Docker, or use --from-tar"
fi

# ------------------------------------------------------------ image lookup

# Both the room lookup and the agent build have to happen as the user who
# ran sudo, not as root: keycard and rustup are almost always installed per
# user, and root's PATH and site-packages know about neither. Running cargo
# as root additionally leaves a root-owned target/ in the checkout, which
# then breaks the user's next plain `cargo build`.
run_as_user() {
    local dir=$1
    shift
    if [ -n "${SUDO_USER:-}" ] && [ "$SUDO_USER" != "root" ]; then
        # -i is what sources the user's profile, and so what puts
        # ~/.cargo/bin and any virtualenv on PATH.
        sudo -u "$SUDO_USER" -i sh -c "cd '$dir' && $*"
    else
        (cd "$dir" && "$@")
    fi
}

# Resolving through keycard.config rather than a local map means a room
# added to keycard.toml is buildable here the same day, with no second
# definition to keep in sync.
resolve_image() {
    run_as_user "$REPO_ROOT" python3 - "$1" <<'PY'
import sys

try:
    from keycard.config import load
except ImportError:
    sys.exit(
        "keycard is not importable; run `pip install -e .` in the repo root, "
        "or pass --image to skip the room lookup"
    )

room = load().rooms.get(sys.argv[1])
if room is None:
    sys.exit(f"no room named {sys.argv[1]!r} in this keycard config")
print(room.image)
PY
}

if [ -z "$FROM_TAR" ] && [ -z "$IMAGE" ]; then
    need python3 "install Python, or pass --image"
    log "resolving room '$ROOM' through keycard's config"
    # Last line only: -i runs a login shell, and a chatty profile would
    # otherwise end up prepended to the image name.
    IMAGE=$(resolve_image "$ROOM" | tail -1)
    [ -n "$IMAGE" ] || die "could not resolve room '$ROOM' to an image"
fi

[ -n "$OUT" ] || OUT="$SCRIPT_DIR/build/$ROOM.ext4"
mkdir -p "$(dirname "$OUT")"

# ------------------------------------------------------------- guest agent

run_tool() {
    run_as_user "$AGENT_DIR" "$@"
}

build_agent() {
    local target=$AGENT_TARGET

    if ! run_tool cargo --version >/dev/null 2>&1; then
        die "cargo not found; build the agent yourself and pass --agent"
    fi

    # rustup, not cargo: the target list belongs to the toolchain manager. A
    # distro cargo with no rustup simply fails both of these and takes the
    # host-target fallback below, which is the right answer there.
    if ! run_tool rustup target list --installed 2>/dev/null | grep -qx "$AGENT_TARGET"; then
        log "adding rust target $AGENT_TARGET"
        if ! run_tool rustup target add "$AGENT_TARGET" >/dev/null 2>&1; then
            # Worth continuing: a glibc agent works fine when the host and
            # the image are close enough in vintage, which they often are
            # during local dev. It is not something to ship a rootfs on.
            echo "build.sh: warning: could not add $AGENT_TARGET; falling back to" >&2
            echo "build.sh: the host target — this agent is dynamically linked and" >&2
            echo "build.sh: will only start if the image's glibc is new enough" >&2
            target=""
        fi
    fi

    if [ -n "$target" ]; then
        log "building guest agent ($target)"
        run_tool cargo build --release --target "$target"
        AGENT_BIN="$AGENT_DIR/target/$target/release/keycard-guest-agent"
    else
        log "building guest agent (host target)"
        run_tool cargo build --release
        AGENT_BIN="$AGENT_DIR/target/release/keycard-guest-agent"
    fi
}

if [ -z "$AGENT_BIN" ]; then
    build_agent
fi
[ -f "$AGENT_BIN" ] || die "no agent binary at $AGENT_BIN"

# --------------------------------------------------------------- unpacking

# Staging goes in TMPDIR, not next to the output: the output often lives on
# a Windows drvfs mount under WSL, and unpacking a root filesystem there
# loses ownership and symlinks.
STAGING=$(mktemp -d "${TMPDIR:-/tmp}/keycard-rootfs.XXXXXX")
CONTAINER=""

cleanup() {
    [ -z "$CONTAINER" ] || docker rm -f "$CONTAINER" >/dev/null 2>&1 || true
    rm -f "$STAGING.tar"
    if [ "$KEEP_STAGING" -eq 1 ]; then
        echo "build.sh: staging left at $STAGING" >&2
    else
        rm -rf "$STAGING"
    fi
}
trap cleanup EXIT

if [ -n "$FROM_TAR" ]; then
    log "unpacking $FROM_TAR"
    tar_src=$FROM_TAR
else
    log "flattening $IMAGE"
    # `docker create` never starts the container, so the command is only
    # there to satisfy images that declare no CMD of their own.
    CONTAINER=$(docker create "$IMAGE" /bin/true)
    docker export "$CONTAINER" -o "$STAGING.tar"
    docker rm -f "$CONTAINER" >/dev/null
    CONTAINER=""
    tar_src="$STAGING.tar"
fi

tar -xf "$tar_src" -C "$STAGING" --numeric-owner --exclude=.dockerenv
rm -f "$STAGING.tar"

# ---------------------------------------------------------------- injecting

if [ ! -x "$STAGING$SHELL_PATH" ]; then
    if [ "$SHELL_EXPLICIT" -eq 1 ]; then
        die "image has no $SHELL_PATH"
    fi
    [ -x "$STAGING$FALLBACK_SHELL" ] || die "image has neither $SHELL_PATH nor $FALLBACK_SHELL"
    log "no $SHELL_PATH in this image; using $FALLBACK_SHELL"
    SHELL_PATH=$FALLBACK_SHELL
fi

log "injecting agent and init"

# /usr/bin and /usr/sbin are real directories whether or not the image is
# usr-merged; /bin and /sbin may be symlinks into them. Installing to the
# real paths keeps the layout predictable, and lets the verify step below
# check for them without depending on how debugfs treats symlinks.
install -D -m 0755 "$AGENT_BIN" "$STAGING/usr/bin/keycard-guest-agent"

mkdir -p "$STAGING/usr/sbin"
sed "s|@KEYCARD_SHELL@|$SHELL_PATH|g" "$SCRIPT_DIR/init.sh" \
    >"$STAGING/usr/sbin/keycard-init"
chmod 0755 "$STAGING/usr/sbin/keycard-init"

# The cmdline should name keycard-init explicitly (see README), but a kernel
# booted without one searches /sbin/init, and finding systemd there would be
# a confusing way to fail.
ln -sf /usr/sbin/keycard-init "$STAGING/sbin/init"

# Mount points init needs to exist before it can mount anything on them.
mkdir -p "$STAGING/proc" "$STAGING/sys" "$STAGING/dev/pts" "$STAGING/dev/shm" \
         "$STAGING/run" "$STAGING/tmp"
chmod 1777 "$STAGING/tmp"

# ----------------------------------------------------------------- building

if [ -z "$SIZE_MB" ]; then
    used_mb=$(($(du -sk "$STAGING" | cut -f1) / 1024))
    # Slack for filesystem metadata and for whatever the session writes; a
    # room with no room to write in is not a usable room.
    SIZE_MB=$((used_mb * 130 / 100 + 64))
    [ "$SIZE_MB" -ge 256 ] || SIZE_MB=256
fi

log "building ext4 (${SIZE_MB} MiB) at $OUT"
rm -f "$OUT"
mke2fs -q -F -t ext4 -L keycard -d "$STAGING" "$OUT" "${SIZE_MB}m"

# ---------------------------------------------------------------- verifying

# Cheap, but it catches the failure that actually happens: mke2fs -d silently
# producing an image without the files, because staging was wrong.
in_image() {
    debugfs -R "stat $1" "$OUT" 2>/dev/null | grep -q '^Inode:'
}

for path in /usr/bin/keycard-guest-agent /usr/sbin/keycard-init "$SHELL_PATH"; do
    in_image "$path" || die "verification failed: $path is missing from $OUT"
done

log "ok — $(du -h "$OUT" | cut -f1) at $OUT"
cat >&2 <<EOF

    shell: $SHELL_PATH
    boot:  init=/usr/sbin/keycard-init console=ttyS0 reboot=k panic=1 pci=off

Not bootable on its own — Phase 2 supplies the kernel. See rootfs/README.md.
EOF
