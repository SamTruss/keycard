# Changelog

All notable changes to keycard are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and versioning follows [Semantic Versioning](https://semver.org/) — see
[CONTRIBUTING.md](CONTRIBUTING.md#releasing) for how a release is cut.

## [Unreleased]

### Added

- `FirecrackerBackend` (v2, `FIRECRACKER.md` Phase 2): rooms as microVMs, the
  pty reached over vsock through the guest agent, and `--keep` implemented
  as a Firecracker snapshot. Booted and verified against real microVMs on a
  host with `/dev/kvm`; the integration tests skip anywhere without one, so
  CI does not exercise it. Not the default and not finished — the microVM
  still has no network and does not run under the jailer, so it is not yet
  the isolation story `SECURITY.md` will eventually tell.
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

### Fixed

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
