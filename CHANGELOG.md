# Changelog

All notable changes to keycard are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and versioning follows [Semantic Versioning](https://semver.org/) — see
[CONTRIBUTING.md](CONTRIBUTING.md#releasing) for how a release is cut.

## [Unreleased]

## [1.0.0] - 2026-08-09

keycard has been installable since 0.1.1 and has shipped nine releases since.
This one doesn't add a feature. It makes a promise.

From here, the config file you wrote, the commands you typed, and the way
`ssh python@yourhost` behaves are **stable** — they will not break on a minor
release. If any of them ever has to change, that's a 2.0, and you'll see it
coming.

Covered: every key in [keycard.example.toml](keycard.example.toml), including
the `[firecracker]` table; `keycard up` and `keycard rooms` with their flags
and the two reserved exit statuses (`124` idle-reaped, `143` shutdown); and
the SSH-facing behaviour — username picks the room, public keys only, `exec`
refused, one channel per connection, room destroyed when the connection ends.

Not covered, so the promise keeps its value: `import keycard` is **not** a
library interface, log lines are for humans rather than parsers, and the
security claims live in [SECURITY.md](SECURITY.md) rather than in semver —
narrowing one is a correction, not a breaking change. The full statement is
in [CONTRIBUTING.md](CONTRIBUTING.md#what-10-promises).

1.0 means you can build on this without watching for the ground to move. It
does not mean the work is finished: tap networking for microVMs is still
[deliberately deferred](FIRECRACKER.md), and the Homebrew formula is still
outstanding.

### Changed

- **`--keep` no longer works on firecracker rooms.** They are destroyed on
  disconnect whatever `keep_window` says. Docker rooms are unaffected.

  Pausing a microVM means snapshotting it, and a Firecracker snapshot writes
  the entire contents of guest RAM to a file — keys, tokens, whatever happened
  to be on screen. Encrypting it helps less than it sounds like it would: the
  VMM restores by reading that file directly, so the plaintext has to exist at
  resume regardless of what is done to it at rest. The tmpfs alternative
  trades disk exposure for a path Landlock then has to admit, widening the
  allowlist that makes the isolation claim strong in the first place.

  That is a poor thing to have sitting inside the one backend whose whole
  pitch is that guest escape is in scope, so it is refused rather than shipped
  with a footnote — and refused *now*, before the freeze, because this is the
  reversible direction. If the snapshot is ever made genuinely secret, turning
  `--keep` back on adds a feature; taking it away after 1.0 would have cost a
  major version.

  In a mixed deployment the server names the excluded rooms at startup, rather
  than leaving an operator to find out by losing a session.

  Upgrading from 0.2.x needs no action unless you were relying on this. Any
  snapshot an older keycard already wrote is cleared at startup: firecracker
  instance directories record their owning processes, and any whose owners are
  all gone are removed. Anything still in use by a live process is left alone.
  The pause machinery is kept rather than deleted for exactly that reason —
  deleting it would have stranded the debris it exists to clean up.

  `SECURITY.md` loses a caveat as a result: a plaintext guest-RAM dump is no
  longer something keycard can produce.

- **`Development Status` is now `5 - Production/Stable`.** It had said
  `2 - Pre-Alpha` since the very first release — already two steps behind what
  the README was claiming by 0.2.4.

## [0.2.4] - 2026-08-09

### Fixed

- **The documented version had drifted two releases behind the package.**
  `README.md`, `man/keycard.1` and both pages under `docs/` still said
  `v0.2.1` after 0.2.2 and 0.2.3 shipped, so the footer of the project page
  and what `man keycard` printed both named a release that was no longer the
  current one. Corrected, and now enforced: `tests/test_version_strings.py`
  fails if any of them disagrees with `keycard.__version__`.

  The cause was that only `pyproject.toml` and `src/keycard/__init__.py` were
  ever checked — `release.yml` verifies the tag against the first, which is
  why those two never drifted, and the release checklist named only those
  two. The other four had nothing watching them. The checklist in
  CONTRIBUTING.md now points at the test rather than at a list of files,
  because a list is the thing that went stale.

## [0.2.3] - 2026-08-09

### Security

- **A crash used to leave a paused room's guest RAM on disk indefinitely.**
  `--keep` snapshots a microVM by dumping its memory to the room's instance
  directory in the clear, and SECURITY.md told operators that dump lived only
  until the keep window expired and the room was reaped. That was true of
  every exit except the one an operator is least able to prevent: the sweep
  ran from `KeptRooms.destroy_all()` on a graceful shutdown, so a `SIGKILL`,
  an OOM kill or a power cut skipped it — and nothing swept at startup
  either, leaving the dump under `runtime_dir` until something else removed
  it.

  Each instance directory now records the keycard process that created it and
  the VMM running in it, and the firecracker backend sweeps at startup,
  before any room opens, removing those whose owners are both gone. The two
  cases have opposite shapes, which is what makes it safe: a *paused* room
  has no VMM — `pause` stops it once the snapshot is written — so the
  directories holding RAM dumps are exactly the ones with nothing running,
  while a *running* room still has its VMM and has taken no snapshot yet. A
  directory a live keycard or a live firecracker still owns is never touched,
  so a second keycard sharing a `runtime_dir` cannot destroy the first one's
  rooms. Only affects the firecracker backend; `--keep` is off by default.

### Changed

- **A `pids_limit` the firecracker backend cannot honour now says so.**
  Setting it on a room that runs on firecracker logs a warning at startup
  naming the room, instead of being dropped in silence. The key genuinely
  does not map — a microVM has its own kernel and its own process table, so
  there is no host-side count to cap, and what actually bounds a guest is
  `memory` and `cpus` — but an operator who writes a limit and reads it back
  in their own config has every reason to think it is in force. Behaviour is
  otherwise unchanged: the key is still ignored, and a config that sets it
  still loads and still starts.

## [0.2.2] - 2026-08-09

### Fixed

- **A room could be left running by a shutdown that raced its own teardown.**
  When a client disconnected moments before `shutdown_grace` expired, its
  room was already being destroyed — or, under `--keep`, paused — on a
  background task. `force_close()` saw the room detached, concluded there was
  nothing to do, and released the session from the drain's registry while
  that teardown was still in flight. The drain then returned, `serve()` ran
  on to `backend.close()` and process exit, and `asyncio.run` cancelled the
  unfinished destroy: a container left running, or a microVM's snapshot —
  guest RAM, in the clear — left on disk with nothing owning it. Under
  `--keep` the pause could also land *after* `KeptRooms.destroy_all()` had
  already swept, so the shutdown sweep found an empty registry.

  Teardown is now a single task per session: whichever trigger gets there
  first owns it, and every other trigger awaits that same task instead of
  racing it, so a session leaves the drain's registry only once its room is
  genuinely gone. This also closes two narrower races of the same shape — the
  read pump's `finally` and a shutdown arriving together could each start a
  destroy, and a room that finished opening after its session had already
  ended was never collected at all. Reachable on every backend and every
  supported Python; the `--keep` half needs `keep_window` set.

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
