# Changelog

All notable changes to keycard are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and versioning follows [Semantic Versioning](https://semver.org/) — see
[CONTRIBUTING.md](CONTRIBUTING.md#releasing) for how a release is cut.

## [Unreleased]

## [0.2.1] - 2026-08-08

Shipped immediately behind 0.2.0, which was tagged but never published to
PyPI — this is the first release carrying the microVM backend.

### Fixed

- **A server shutdown hung, and leaked rooms, whenever anyone was connected.**
  `serve()` awaited the listener's `wait_closed()` *before* draining active
  sessions, and `asyncio.Server.wait_closed()` does not return until every
  accepted connection has dropped as well as the listener — so shutdown
  deadlocked against the one thing that closes those connections. In
  practice: SIGINT/SIGTERM logged "shutting down" and then sat there with
  containers still running until the last client happened to disconnect on
  its own, making `shutdown_grace` meaningless exactly when it mattered.
  The drain now runs first, and the wait after it is bounded so a client
  that never drops its TCP connection cannot keep the process alive once
  its rooms are already gone. Present since 0.1.0 but only reachable on
  Python 3.12.1+, where `wait_closed()` gained its current semantics — on
  3.11 it returned immediately, which is why nothing caught it sooner.
- A session whose channel died mid-teardown raised an unretrieved
  `BrokenPipeError` into the event loop, because `is_closing()` can still
  report False on a channel that is already past sending. The room was
  always destroyed correctly; this was noise in the log at exactly the
  moment an operator is reading it.

## [0.2.0] - 2026-08-08

The microVM backend, and the first real isolation claim keycard has made.
Opt-in, and the Docker backend is untouched and still the default — an
existing install behaves exactly as it did on 0.1.3.

### Added

- `FirecrackerBackend` (v2): rooms as microVMs, the pty reached over vsock
  through a guest agent, and `--keep` implemented as a Firecracker
  snapshot. Booted and verified against real microVMs on a host with
  `/dev/kvm`; the integration tests skip anywhere without one, so CI does
  not exercise it. Opt-in via `backend = "firecracker"`, and it needs a
  guest kernel and a rootfs built by `rootfs/build.sh`. A microVM room has
  no network at all — see `FIRECRACKER.md` for why that is deliberate.
- Backend selection: a `backend` key at the top level and per room, so a
  deployment can mix the two. Rooms that don't name one use the default.
- A `[firecracker]` config table (guest kernel, rootfs directory, runtime
  directory, boot timeout), only read when something selects that backend.
- The guest agent's TCP smoke test now runs in CI, covering the bridge end
  to end against a real shell — including reconnecting to a session whose
  host dropped, which is what `--keep` rests on.
- The firecracker process is now confined with Landlock (Linux 5.13+) to
  its own instance directory, the guest kernel and `/dev/kvm`, so a
  compromised VMM can no longer read the SSH host key or any other room's
  disk image. Firecracker's own jailer would have done this, but it has to
  run as root; this needs no privilege at all. Configurable with
  `[firecracker] confinement`, which also takes `required` for deployments
  that would rather not start than run unconfined.

### Changed

- **`SECURITY.md` now claims a real isolation boundary for microVM rooms.**
  Guest escape is in scope for rooms opened under `backend = "firecracker"`
  — the first time keycard has claimed anything of the sort, and the thing
  v2 existed to deliver. The claim is scoped to rooms with no network,
  which is every microVM room, and each condition it rests on is asserted
  against a booted VM rather than stated. Two caveats are documented rather
  than fixed, because both are real: a `--keep` snapshot writes guest RAM
  to disk unencrypted, and both backends share one SSH front door.
- `SECURITY.md` gained an "out of scope on any backend" section, so the
  exclusions that never depended on which backend you chose are stated once
  instead of buried in the Docker one. It says plainly that reports which
  depend on keycard having been run as root, under `sudo`, as an
  Administrator, or with added capabilities **will not be investigated** —
  nothing in keycard needs privilege, and every other guarantee on that
  page assumes it doesn't have any.

### Fixed

- Instance directories were created world-readable (`0755`) under a default
  runtime directory of `/tmp/keycard`, so any local user on the host could
  read a room's whole disk image — and, once `--keep` had paused it, a
  plaintext dump of its RAM. They are now `0700`, and an existing runtime
  directory is tightened rather than trusted. Only reachable by someone
  with a local account on the host, and only for the Firecracker backend,
  which has never been in a release.
- A room whose shell exited hung instead of ending. The guest agent
  half-closed its side of the vsock connection and Firecracker relays that
  to nothing, so the host's read never returned EOF and the session sat
  there until something else timed it out. The agent now drops the
  connection outright. Invisible to CI, which stands vsock in with TCP —
  and TCP does propagate a half-close.
- Input typed while a room was still opening was discarded rather than
  queued, so the first command of a session could vanish. A container
  opens fast enough that this almost never showed; a microVM boot is long
  enough that it showed every time. A window resize during the same window
  was dropped the same way, and now sets the size the room opens at.
- The guest agent started its shell with `--login`, which `dash` rejects
  outright — so any room whose image has no bash (a case `rootfs/build.sh`
  supports deliberately) would have had its shell exit the instant it
  started. Uses `-l`, which both shells accept. Found before the first
  boot, so no release ever shipped it.

## [0.1.3] - 2026-08-07

### Fixed

- Several README links (license, usage guide, security policy,
  contributing guide, architecture doc) were relative paths that only
  resolve on GitHub — on PyPI, where only the README itself is uploaded,
  they 404'd. Converted to absolute GitHub URLs.
- Added a `Changelog` link to PyPI's project links sidebar.

## [0.1.2] - 2026-08-07

### Fixed

- README still said "Pre-alpha. Not released, not usable yet" and told
  installers to run `pipx install keycard-ssh` "once released" — baked into
  the 0.1.1 tag before that release actually shipped to PyPI, so the
  published package description contradicted itself. No functional changes.

## [0.1.1] - 2026-08-07

### Added

- SSH server with public-key authentication, username-to-room resolution at
  auth time, PTY allocation, and window-resize propagation.
- Built-in rooms (`ubuntu`, `python`, `node`) that work with zero config,
  plus an optional `keycard.toml` for custom rooms and a default-room
  fallback for unrecognised usernames.
- Per-room resource caps (memory, CPU, pids) and network isolation
  (`network = "none"`).
- Idle-timeout reaper: a room with no traffic in either direction for
  `idle_timeout` is destroyed automatically.
- `--keep` / `keep_window`: pause a room on a dropped connection instead of
  destroying it, and let the same username reconnect and resume it within
  the window.
- Graceful server shutdown: on SIGINT/SIGTERM, stop accepting new
  connections, give active sessions `shutdown_grace` to finish on their
  own, then close out whatever's left.
- `keycard rooms` to list configured environments; ASCII banners on the
  operator console and in the SSH session itself.
- A man page (`man/keycard.1`).

### Fixed

- Containers no longer survive a dropped connection: teardown polls
  container state instead of blocking on `container.wait()`, with a grace
  period and kill fallback, and is idempotent under concurrent triggers
  (client exit, connection drop, idle reap, and shutdown can all race).
