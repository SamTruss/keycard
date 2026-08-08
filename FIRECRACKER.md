# Firecracker Backend — Phased Plan

Status: **Phases 0, 1 and 2 done** (`guest-agent/`, `rootfs/`,
`backends/firecracker.py`). Phase 2 has now booted real microVMs:
`tests/test_firecracker_integration.py` runs green on a WSL2 host with
`/dev/kvm`, firecracker 1.16.1 and a 6.1 `vmlinux`. Phases 3–5 not started.
This is the roadmap referenced by `SCOPE.md`'s v2 section, written down for
the first time — previously only discussed, never committed.
Nothing below is final; treat it as the current best guess, revised as work
actually starts.

## Why this exists

v1's Docker backend is not a security boundary — see `SECURITY.md`'s honest
threat model: namespace and cgroup isolation, not hardware isolation, and a
container escape is a container escape. Firecracker adds a real boundary,
hardware-virtualized via KVM, without giving up the "one command, zero
config" story on the client side.

## Already decided

**The seam already exists.** `backends/base.py`'s `Backend`/`Room`/`Kept`
was designed for this from day one (see `ARCHITECTURE.md`, "The backend
seam"). A `FirecrackerBackend` implementing those three classes should be a
drop-in for `DockerBackend` with zero changes to `server.py` or
`session.py`. If building it turns out to require a change there, that's a
sign the abstraction has a gap worth fixing first, not a reason to special-case.

**Guest shell over vsock + a small guest agent — not sshd in the guest.**
Running sshd inside every microVM means a second auth handshake, per-guest
host keys to generate and discard, and a second copy of the attack surface
keycard exists to shrink. keycard already owns the outer SSH session and
already authenticated the client; the guest doesn't need to do that again.
Instead: a minimal agent inside the guest, listening on a vsock port, that
on connect execs a shell with a pty and bridges bytes — conceptually the
same shape as `DockerRoom`'s attach-socket bridge, just a vsock socket
instead of a Unix socket.

**Rootfs bootstrapped from the existing Docker images**, not a second
image-build pipeline. The ubuntu/python/node room definitions stay the
source of truth; the Firecracker rootfs is a derived artifact of them, so
`keycard.toml` semantics don't fork between backends.

## Hard prerequisite

Real `/dev/kvm` access — nested virtualization or bare metal. GitHub Actions
Ubuntu runners don't expose it, so integration coverage here needs a
self-hosted runner before it's real. Don't fake it by mocking the KVM
boundary away; that defeats the point of testing a security boundary.

### Making a host eligible

`tests/test_firecracker_integration.py` checks all of this and names the
first thing missing, so the fastest way to find out where a host stands is
to run it.

1. **Hardware virtualization exposed to the OS.** `grep -oE 'vmx|svm'
   /proc/cpuinfo | head -1` prints something, and `ls -l /dev/kvm` exists.
   WSL2 on a recent Windows 11 build does expose it, which makes a Windows
   dev box a viable Phase 2 host — it was assumed not to be for a long time,
   so check rather than assume.
2. **Read/write on `/dev/kvm`.** `sudo usermod -aG kvm $USER`, then start a
   new login session (`wsl --shutdown` on WSL). Membership doesn't apply to
   already-running shells.
3. **The firecracker binary on `PATH`.** A release download is enough; there
   is no daemon and nothing to configure.
4. **A guest kernel** — an uncompressed `vmlinux`, not a distro `bzImage`.
   Firecracker boots ELF kernels directly with no bootloader.
5. **A rootfs** — `sudo rootfs/build.sh --room ubuntu` writes
   `rootfs/build/ubuntu.ext4`. Put it where `[firecracker] rootfs_dir`
   points, named after the room.

Then:

```bash
export KEYCARD_TEST_KERNEL=/var/lib/keycard/vmlinux
export KEYCARD_TEST_ROOTFS_DIR=/var/lib/keycard/rootfs
pytest tests/test_firecracker_integration.py -v -rs
```

When a boot fails, the guest console is in `console.log` inside the
instance directory under `[firecracker] runtime_dir`. It is usually the only
thing that says why.

## Phases

### Phase 0 — Guest agent, no Firecracker yet — **done**

`guest-agent/`: a small Rust binary (static-ish release build, no async
runtime overhead beyond tokio itself — see that directory's README for why
Rust over Go). Meant to run as PID 1's direct child with no other runtime
dependencies in the rootfs. Listens on a data port (raw pty bytes) and a
control port (newline-delimited `resize`/`exit` messages), execs the shell
on connect, bridges the two, reports exit status.

Verified with `--transport tcp` as a loopback stand-in for vsock — no
microVM needed for this phase (see `guest-agent/test_client.py`): spawns a
real shell, sends input, resizes mid-session and confirms `TIOCSWINSZ` took
effect via `stty size`, confirms the exit code round-trips over the control
channel. `--transport vsock` (the real path) is unverified — that needs
Phase 2 and a `/dev/kvm`-capable host.

### Phase 1 — Rootfs build — **done**

`rootfs/build.sh`: takes an existing room's Docker image, flattens it to an
ext4 rootfs, and injects the Phase 0 agent as what PID 1 execs. The room's
image is resolved through `keycard.config`, so `keycard.toml` stays the
single definition of what a room is. See `rootfs/README.md`.

The init question above is settled the way it leaned: a shell script
(`rootfs/init.sh`) that mounts the pseudo-filesystems and execs the agent,
no tini. The cost is that the agent, as PID 1, reaps only its own child, so
orphans become zombies for the life of the microVM — bounded by the VM's
own life, and cheap to revisit if a room ever accumulates enough of them to
matter.

CI builds a real `ubuntu:24.04` rootfs on every PR and checks the agent and
init are in the image and that the agent is statically linked. The image is
now also known to boot: Phase 2's integration tests run against exactly this
artifact, though only on a host with `/dev/kvm`, which CI is not.

### Phase 2 — `FirecrackerBackend` — **booted and verified**

`backends/firecracker.py` implements `Backend`/`Room`/`Kept` against the
Firecracker REST-over-UNIX-socket control API. It went in as predicted:
`session.py` and `server.py`'s session handling did not change, so the seam
held.

- `Room.read`/`write` drive the vsock connection to the Phase 0 agent,
  `loop.sock_recv`/`sock_sendall` directly on the unwrapped socket — the same
  shape as `DockerRoom`, as expected.
- `Room.pause()` / `Kept` / `Backend.resume()`: `pause()` pauses the VM,
  snapshots it to disk, and stops the process; `resume()` launches a fresh
  firecracker and loads the snapshot back. Unlike Docker's freezer this
  actually frees the guest's RAM, at the cost of writing it out.
- `Room.destroy()`: stop the process, remove the instance directory. Kept
  idempotent under the same contract `DockerRoom.destroy()` documents.

Everything a microVM owns — API socket, vsock socket, the room's rootfs
copy, its snapshot, the guest console log — lives in one instance directory,
so tearing a room down is one `rmtree` and there is no second store to reap.

Three pieces of the wire format are not obvious and cost real time to get
right, so they are called out here rather than left in the code:

- The host must connect to the guest agent's **data port before its control
  port**. The agent accepts in that order; reversing it deadlocks both sides.
- Firecracker's vsock handshake reply must be read **one byte at a time**. A
  buffered read swallows whatever session bytes arrived behind the newline,
  and those are the first thing the shell ever said.
- `PUT /snapshot/load` must be the **first call** on a fresh API socket. It
  restores machine config, drives and the vsock device wholesale, and
  Firecracker rejects it once anything else has been configured.
- **A guest half-close is not relayed.** Firecracker closes the host's end
  of a vsock connection only when the guest end closes *completely*. The
  agent calling `shutdown(WR)` on the data channel returns `Ok` and reaches
  the host as nothing at all, so `Room.read()` never sees EOF and a room
  whose shell has exited hangs. The guest has to drop both halves — see
  `detach` in `guest-agent/src/session.rs`. This is the one thing on this
  list that no amount of unit testing found; it only appears against a real
  microVM, because the TCP stand-in *does* propagate a half-close.

**What is verified.** The wire format, the handshake parsing, the memory and
vCPU mapping, the instance layout and the preflight failures are unit-tested
(`tests/test_fcapi.py`, `test_vsock.py`, `test_firecracker.py`). The guest
agent's bridge — including the reattach path `--keep` depends on — is tested
end to end against a real shell over the TCP stand-in, in CI.

**What the first boot changed.** All ten integration tests pass, but only
after two bugs that no unit test could have found, both of which needed a
real microVM — and, between them, the reason to distrust "written but never
run":

- The guest agent half-closed the data channel on shell exit (above). Every
  room hung after its shell exited.
- `session.py` dropped client input that arrived before `open()` returned.
  A Docker room opens in about a second so nothing was ever typed into that
  window; a microVM boot is long enough that the *first command of every
  session* landed in it and was discarded. Input is queued now and flushed
  once the room exists.

Both have regression tests that fail against the old code
(`an_exiting_shell_reports_and_closes` asserts a full close, not just a
shutdown; `test_input_typed_while_the_room_opens_is_not_lost` uses a
deliberately slow backend).

**What is still not verified.** CI has no `/dev/kvm`, so
`test_firecracker_integration.py` still skips there and the boot path is
only covered on a host that qualifies. A self-hosted runner is what would
change that.

### Phase 3 — Tap networking

Per-room tap device and IP, matching the isolation intent of today's
`network = "none"` default. Keep the default identical to Docker rooms —
no network unless a room opts in — don't let backend choice change a room's
default posture.

### Phase 4 — Resource limits parity

Map `RoomConfig`'s memory/cpus onto Firecracker's machine-config
(`mem_size_mib`, vcpu count) and cgroups via the jailer. `pids_limit`
doesn't map 1:1 — a microVM isn't sharing the host's process table with
anything — so it needs its own decision: drop it for this backend, or
reinterpret it as a limit the guest agent itself enforces.

### Phase 5 — Update the security claim

Only after the above is real and integration-tested: update `SECURITY.md`'s
threat model to state actual VM-level isolation for rooms opened under the
Firecracker backend — kept distinct from the Docker backend's
namespace/cgroup-only claim, not blurred into one blanket statement. This
is the phase that actually delivers on "v2 adds a real isolation boundary";
don't claim it earlier.

## Questions this phase settled

- **jailer vs. raw `firecracker` process** — *raw first, jailer before Phase
  5.* The isolation boundary is KVM either way; the jailer hardens the host
  against a compromised firecracker process, which is defence in depth rather
  than the boundary itself. Writing its chroot/cgroup/seccomp setup before
  anything had ever booted would have meant debugging two unverified layers
  at once. `firecracker_argv()` is the seam it slots into.
- **Snapshot storage lifecycle** — *no separate store.* Everything one
  microVM owns lives in a single instance directory, the snapshot and memory
  file included, so `destroy_kept()` reclaims the disk with one `rmtree`.
- **Backend selection** — *both.* A per-room `backend = "firecracker"` key,
  with a top-level `backend` as the default. The config key was never the
  expensive part: `KeptRooms` calls `destroy_kept()` on a paused room long
  after its session is gone, so routing needs a registry that can find the
  backend a given `Kept` came from. Once that exists, a server-level default
  is a fallback lookup. See `backends/routing.py` — the registry is itself a
  `Backend`, so nothing above the seam learned that there is more than one.

## Still open

- **`pids_limit` for a microVM** (Phase 4). It doesn't map: a guest isn't
  sharing the host's process table with anything. Drop it for this backend,
  or reinterpret it as something the guest agent enforces.
- **Trailing pty output on exit.** The agent gives the pty a 50ms drain
  before closing the data channel, which is a mitigation, not a fix. The
  proper answer is to read the pty to EOF, which risks hanging on an orphan
  holding the slave open.

## Sequencing

Phases 0 and 1 were done without the KVM prerequisite, as expected — neither
boots anything. Phase 2 was written on the same terms: everything that could
be decided and tested without a microVM was, and the rest waited on a host.
That was worth doing rather than blocking, because the parts most likely to
be wrong (the wire format, the handshake, the ordering constraints) are
exactly the parts that don't need one.

That bet paid off only partly, and the shape of the miss is worth keeping.
Every guess about the *wire format* was right and survived first contact.
Both bugs the first boot found were about **lifecycle** — when a connection
ends, and when a room starts — which is precisely what a stand-in transport
and a fast backend hide. Phases 3–5 are now unblocked, but the lesson
generalises to them: the timing-dependent parts of tap networking will not
be provable without a booted VM either.
