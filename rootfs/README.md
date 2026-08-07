# keycard rootfs build

Phase 1 of `FIRECRACKER.md`. Turns a keycard room's Docker image into an
ext4 filesystem a Firecracker microVM can boot, with the Phase 0 guest agent
inside it.

Not wired into the shipped server. `DockerBackend` is still the only backend
keycard has; this produces the disk image Phase 2's `FirecrackerBackend`
will boot.

## Building

```bash
sudo rootfs/build.sh --room ubuntu
```

Writes `rootfs/build/ubuntu.ext4`. The room name is resolved through
keycard's own config loader, so a room added to your `keycard.toml` is
buildable immediately — there is no second image list here to keep in sync.

```
--room NAME     room to build (default: ubuntu)
--image IMAGE   Docker image directly, skipping the room lookup
--from-tar PATH pre-flattened rootfs tar instead of a Docker image
--out PATH      output image (default: rootfs/build/<room>.ext4)
--size MB       filesystem size (default: content + 30%, minimum 256)
--agent PATH    prebuilt guest agent (default: cargo build it)
--shell PATH    shell the agent execs (default: /bin/bash)
--keep-staging  leave the unpacked tree on disk to poke at
```

### Requirements

- Linux, and root — see "Why root" below.
- Docker, for everything except `--from-tar`.
- `e2fsprogs` 1.43 or newer, for `mke2fs -d`.
- A Rust toolchain, unless you pass `--agent`.

No `/dev/kvm`. Nothing in Phase 1 boots anything, which is why this phase
could be finished on a machine that cannot run Phase 2.

## What ends up in the image

- The room image's userland, flattened — no layers, no Docker metadata.
- `/usr/bin/keycard-guest-agent`, the Phase 0 agent, statically linked.
- `/usr/sbin/keycard-init`, the init from `init.sh`, with the shell path
  substituted in.
- `/sbin/init` symlinked to it, so a kernel booted without an explicit
  `init=` still lands in the right place instead of on the image's systemd.

## Booting it (Phase 2's job)

The image is a root filesystem and nothing else — no kernel, no bootloader.
Phase 2 supplies both. The cmdline it will want:

```
init=/usr/sbin/keycard-init console=ttyS0 reboot=k panic=1 pci=off
```

`panic=1` matters: `keycard-init` execs the agent, so the agent *is* PID 1,
and if it exits the kernel panics. That is the intended behaviour — the room
has ended, and the microVM going away is exactly the signal the backend
wants. Without `panic=1` the guest sits there panicked instead of halting.

## Why it is built this way

**`mke2fs -d`, not a loop mount.** The usual recipe for this is to
`truncate` a file, `mkfs` it, `mount -o loop`, and copy the tree in. That
needs a free loop device and the privileges to attach one, which rules out
most containerised CI. `mke2fs -d` builds the filesystem from a directory
tree directly, so the same command works on a laptop and on a stock GitHub
runner — which is what lets CI build a real rootfs on every PR.

**Why root anyway.** Unpacking a container filesystem preserves ownership
and device nodes; an unprivileged user cannot recreate either, and a rootfs
where everything is owned by uid 1000 is not the rootfs the image describes.
The script drops back to the invoking user (`$SUDO_USER`) for the room
lookup and the agent build, so `sudo` does not leave a root-owned `target/`
in your checkout or go looking for keycard in root's site-packages.

**A static agent.** The agent is built on the host but runs against the
*image's* libc, and those are rarely the same vintage — a glibc build on a
newer host produces a binary the guest silently cannot start. Building for
`x86_64-unknown-linux-musl` removes the coupling. If that target is not
installed and cannot be added, the script warns and falls back to a
dynamically linked build; that is fine for poking around locally and is not
something to ship a rootfs on.

**A shell script for init, not tini.** `FIRECRACKER.md` left this open and
leaned toward the smaller option, which is what this is. A room runs one
process tree for one session, so there is nothing to supervise — the init's
whole job is mounting `/proc`, `/sys`, `/dev/pts` and friends and then
`exec`ing the agent.

The known cost: because the init `exec`s, the agent is PID 1, and it only
reaps its own child. Anything the session orphans becomes a zombie for the
life of the microVM. Nothing leaks past the room's life, since the VM is
destroyed wholesale, so this is a wart rather than a leak — but it is the
thing to fix (with tini, or by reaping in the agent) if a room ever
accumulates enough zombies to matter.

## Testing

CI builds a real `ubuntu:24.04` rootfs on every PR and checks the agent and
init are in the resulting image, and that the agent is statically linked.
That is the part a Windows dev box cannot do locally, since it needs a
Docker daemon.

`--from-tar` skips Docker entirely and takes a pre-flattened rootfs tar,
which is enough to exercise everything else — the injection, the sizing, the
`mke2fs` call, and the verification — on a host with no daemon running.

What is *not* tested anywhere yet: whether the resulting image actually
boots. That needs Phase 2 and a `/dev/kvm`-capable host.
