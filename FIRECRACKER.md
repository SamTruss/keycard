# Firecracker Backend — Phased Plan

Status: **Phase 0 done** (`guest-agent/`, merged in #31). Phases 1–5 not
started. This is the roadmap referenced by `SCOPE.md`'s v2 section, written
down for the first time — previously only discussed, never committed.
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
Ubuntu runners don't expose it, so:

- Dev work needs a bare-metal Linux box or a cloud instance with nested
  virt enabled (GCP/Azure support this; check before assuming AWS does for
  the instance type in use).
- CI can't run this backend's integration tests on the current runner
  matrix. Mirror the existing Docker split (`_docker_available()` gates
  `test_integration.py`) with an equivalent availability check, and accept
  that integration coverage here needs a self-hosted runner before it's
  real — don't fake it by mocking the KVM boundary away, that defeats the
  point of testing a security boundary.

Nothing past Phase 0 can be verified without this. Confirm the dev
environment before investing in Phase 1+.

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

### Phase 1 — Rootfs build

A script (not a manual process) that takes an existing room's Docker image,
flattens it to an ext4 rootfs, and injects the Phase 0 agent as what PID 1
execs. Open question: minimal custom init that execs the agent directly, vs.
a tiny existing init (tini) supervising it for reaping zombies. Lean toward
the custom init first — fewer moving parts — and only add tini if orphaned
processes inside the guest turn out to matter.

### Phase 2 — `FirecrackerBackend`

Implements `Backend`/`Room`/`Kept` against the Firecracker REST-over-UNIX-socket
control API.

- `Room.read`/`write` drive the vsock connection to the Phase 0 agent — same
  shape as `DockerRoom.read`/`write`'s `loop.sock_recv`/`sock_sendall`
  directly on the unwrapped socket; that pattern should translate with
  little change.
- `Room.pause()` / `Kept` / `Backend.resume()`: Firecracker supports
  snapshot/restore natively. `pause()` snapshots to disk and `Kept` carries
  the snapshot path; `resume()` restores from it. This is the microVM
  equivalent of Docker's pause/unpause and should slot into the existing
  `--keep` machinery (`KeptRooms` in `session.py`) unchanged — that registry
  doesn't know or care what's inside a `Kept`.
- `Room.destroy()`: stop the microVM process, clean up the rootfs
  copy-on-write layer and any snapshot. Must stay idempotent under the same
  contract `DockerRoom.destroy()` documents — the "many ways a room ends"
  table in `ARCHITECTURE.md` applies unchanged to this backend.

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

## Open questions

- **jailer vs. raw `firecracker` process.** Jailer is upstream's hardened
  default (seccomp + chroot + cgroup setup) but adds real setup complexity.
  Default to jailer unless it blocks early dev velocity.
- **Snapshot storage lifecycle.** Unlike Docker's pause (near-zero cost),
  a Firecracker snapshot is real disk. `--keep` window expiry needs to
  reap these promptly — `KeptRooms._expire()` already does the timing,
  but the Firecracker `destroy_kept()` needs to actually free the disk,
  not just stop a process.
- **Backend selection.** Global server flag, or a per-room `backend =
  "firecracker"` key in `keycard.toml`? Per-room is more flexible but adds
  a config surface; needs a decision before `cli.py`/`config.py` changes.

## Sequencing

Phase 0 done without the KVM prerequisite, as expected. Phase 1 (rootfs
build) doesn't strictly need `/dev/kvm` either — it's just a build script —
but Phase 2 (`FirecrackerBackend`) can't be verified end to end without a
`/dev/kvm`-capable host. Confirm that environment before investing much in
Phase 2. No other in-flight work depends on any of this.
