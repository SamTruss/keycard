# Firecracker Backend — Phased Plan

Status: **every phase done except Phase 3, which is deferred on purpose**
(`guest-agent/`, `rootfs/`, `backends/firecracker.py`,
`backends/confine.py`). Phase 2 boots real microVMs:
`tests/test_firecracker_integration.py` runs green on a WSL2 host with
`/dev/kvm`, firecracker 1.16.1 and a 6.1 `vmlinux`. Phase 4 confines the
VMM without the root the jailer would have needed. Phase 5 makes the
isolation claim, scoped to rooms with no network — which is all of them,
because Phase 3 is deliberately unbuilt; see the note above it. This is the
roadmap referenced by `SCOPE.md`'s v2 section, written down for the first
time — previously only discussed, never committed.
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

> **The order of the remaining phases changed after Phase 2 shipped.** The
> numbers below are names, not a sequence. The work order is 4, then 5, then
> 3 — confine the VMM, then a security claim scoped to networkless rooms,
> then networking if and when it earns its cost. Why is under Phase 3.
> Phases 4 and 5 are done. Only Phase 3 remains, and it is deferred.

### Phase 3 — Tap networking — **deferred, deliberately**

Per-room tap device and IP, matching the isolation intent of today's
`network = "none"` default. Keep the default identical to Docker rooms —
no network unless a room opts in — don't let backend choice change a room's
default posture.

Deferred behind Phases 4 and 5, on purpose. Both ways of building it spend
real security to buy something no current use case needs, and a microVM
with no NIC at all is a boundary that is both stronger and free:

- **keycard creates taps and NAT rules itself.** Needs `CAP_NET_ADMIN`,
  which is close to root-equivalent (rewrite netfilter, redirect routes,
  promiscuous mode, `tc`/BPF). Worse, it cannot be dropped after startup
  the way a privileged port bind can — rooms open on connect, so the
  capability is live while unauthenticated SSH input is being parsed. It
  also needs host-wide `ip_forward`, and it puts the privilege in the one
  component the jailer does not confine. If this is ever built, it needs
  privilege separation: a tiny helper forked *before* the listener starts,
  whose whole vocabulary is create/delete tap, à la OpenSSH privsep or
  `qemu-bridge-helper`.
- **Operator pre-provisions a bridge and a pool of taps.** keycard stays
  unprivileged, but the boundary moves into configuration keycard cannot
  see or verify — the same `keycard.toml` is isolated or sitting on the
  corporate LAN depending on someone else's bridge. A shared bridge also
  puts every guest in one broadcast domain (guest-to-guest reachability,
  ARP/MAC spoofing, IPv6 RA spoofing) and persistent taps carry state
  across rooms, so "the room is gone" stops being true at the network
  layer. Pool exhaustion becomes a hard concurrency ceiling.

Note the inversion: the unprivileged option lands on the *weaker* topology,
because per-room routed links need route manipulation, which needs the
privilege that made the other option unattractive.

Today `network = "none"` is an absolute guarantee — there is no NIC, so
there is nothing to argue about. That is worth more than the feature.

### Phase 4 — Confining the VMM — **done, without the jailer**

**The jailer needs root.** Run it as anyone else and it stops at
`Failed to unshare into new mount namespace: Operation not permitted`. That
was checked, not assumed, and it invalidated the reason this phase was
pulled forward — the plan had it down as costing keycard no new privilege.

But the jailer's model is "you are root, let me drop you safely", and
keycard already starts where that ends. Measuring what it would actually
have added:

| jailer gives | already true |
|---|---|
| seccomp filtering | **yes** — firecracker installs its own by default; `--no-seccomp` is the opt-out |
| an unprivileged uid | **yes** — the VMM inherits keycard's user |
| chroot | no |
| cgroups | no |
| `fsize`/`no-file` rlimits | no, but `setrlimit` needs no privilege |

So the gap was filesystem confinement and cgroups, not privilege dropping.
The gap that mattered: without confinement a compromised VMM runs as the
keycard user, and can read every other room's rootfs and
`~/.config/keycard/host_key`.

That is now closed by **Landlock** (`backends/confine.py`) — an unprivileged
LSM, inherited across `execve`, which cuts the VMM down to its own instance
directory, the guest kernel and `/dev/kvm`. The allowlist is short because
firecracker is statically linked: no loader, no libraries. Configurable via
`[firecracker] confinement`, defaulting to best-effort so a kernel without
Landlock still runs, with `required` for anyone who would rather not.

Two things Landlock does not cover, both deliberate:

- **cgroups.** Unprivileged access means `systemd-run --user --scope`, which
  needs a user D-Bus session a system service does not have, and wrapping
  the VMM in a supervisor complicates every signal path in `_stop`. Guest
  RAM is already capped by `mem_size_mib`, so what is uncovered is VMM
  overhead, not guest allocation.
- **`pids_limit`.** Still has no meaning for a microVM and is still ignored,
  but no longer in silence: `config.py` warns at load when a firecracker room
  sets it, naming the room. The decision it was waiting on went to "say so"
  rather than "enforce it" — see Still open below for why enforcement was not
  worth its cost.

### Phase 5 — Update the security claim — **done**

`SECURITY.md` now states VM-level isolation for rooms opened under the
Firecracker backend, kept as its own section rather than blurred into one
blanket statement with the Docker backend's namespace-only posture. Guest
escape is in scope.

The claim is **scoped to rooms with no network**, which is every Firecracker
room. That is not a weaker version of the promise — a guest with no NIC is
the easiest boundary there is to defend, and "run something you don't fully
trust" (see the `offline` room in `keycard.example.toml`) wants less
network, not more. Phase 3 will therefore spend a claim already held rather
than gamble one not yet earned.

Each condition the claim rests on is checked rather than asserted: the
guest has no network interface (asserted against a booted VM in
`test_firecracker_integration.py`, reading `/sys/class/net` so a rootfs
without `iproute2` still answers), the VMM is Landlock-confined (asserted
by denial in `test_confine.py`, and read off a live VMM's `NoNewPrivs`),
and the guest reaches the host only through vsock.

**Writing the claim found a bug**, which is the argument for writing claims
down. Instance directories were created `0755` under a default runtime
directory of `/tmp/keycard`, so every local user on the host could read a
room's disk image — and, after `pause`, a plaintext dump of its RAM. Now
`0700`, with an existing directory tightened rather than trusted, since it
lives somewhere anyone can create first.

One caveat is stated in `SECURITY.md` rather than fixed, because it is real:
both backends sit behind the same SSH front door, where a pre-auth flaw is a
compromise regardless of backend. The other — a `--keep` snapshot writing
guest RAM to disk unencrypted — was closed by refusing the feature here; see
below.

**`--keep` is refused on this backend.** Pausing a microVM means snapshotting
it, and Firecracker restores from the memory file by reading it directly, so
the plaintext has to exist at resume whatever is done to it at rest. That
makes encryption a large amount of work for a guarantee weaker than it looks,
and the tmpfs variant trades disk exposure for a path Landlock then has to
admit — widening the confinement allowlist that makes the claim strong in the
first place. Refusing costs a reconnect window on an opt-in feature of an
opt-in backend, and it is the reversible direction: making the snapshot
genuinely secret later would re-enable a feature rather than break one.

The pause/resume machinery is deliberately **not** deleted along with the
feature. A host upgraded from 0.2.x can still hold snapshots an older keycard
wrote, and `reap_debris` is what clears them — both that sweep and the tests
proving it works need a way to produce a paused room.

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

- ~~**`pids_limit` for a microVM**~~ — **decided.** It doesn't map: a guest
  isn't sharing the host's process table with anything, so there is no
  host-side count to cap. The two candidates were to have the guest agent
  enforce it, or to keep ignoring it and say so. Enforcement lost: it would
  mean a guest-side `RLIMIT_NPROC` or cgroup, a rootfs rebuild and new
  integration tests, to bound something that cannot reach the host in the
  first place — a fork bomb in the guest costs that guest its own room, and
  nothing else. The ceilings that actually matter are `memory` and `cpus`,
  which Firecracker applies to the VM. So the key stays ignored, and
  `config.py` now warns at load when a firecracker room sets it. That turns
  a documented gap into a loud one, which is the honest version of the same
  behaviour.
- **Trailing pty output on exit.** The agent gives the pty a 50ms drain
  before closing the data channel, which is a mitigation, not a fix. The
  proper answer is to read the pty to EOF, which risks hanging on an orphan
  holding the slave open.
- ~~**A `--keep` snapshot is still unencrypted at rest.**~~ **Settled: the
  feature is refused on this backend.** See "Questions this phase settled"
  below.

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
and a fast backend hide. The remaining phases are unblocked, but the lesson
generalises to them: the timing-dependent parts of tap networking will not
be provable without a booted VM either.

The remaining order changed once Phase 2 was real. Networking was
originally next because the plan treated it as part of reaching parity with
the Docker backend. Looking at what it actually costs — a privileged
keycard, or a boundary defined by configuration keycard cannot verify —
against what it buys, the answer was to take the claim that is already
sitting there unclaimed. A networkless microVM is the strongest thing this
project has ever been able to say about isolation, and nothing had to be
built to make it true.
