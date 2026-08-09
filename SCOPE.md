# Project Scope

keycard is a purpose-built SSH server that provisions disposable containers on connect and destroys them on disconnect. The username selects the environment. No client to install, no configuration required.

## v1 — Docker / Podman

### Core

- [x] SSH server with public key authentication (AsyncSSH)
- [x] Host key generation on first run (ed25519, 0600 permissions)
- [x] Username → image resolution at auth time
- [x] PTY allocation and window resize propagation
- [x] Exit code forwarding through the bridge
- [x] Container teardown on clean disconnect
- [x] Container teardown on dropped connection
- [x] Proper shell prompt in all rooms

### Configuration

- [x] Built-in rooms (ubuntu, python, node) with zero config
- [x] Optional `keycard.toml` for custom rooms
- [x] Default room fallback for unrecognised usernames
- [x] CLI: `keycard up` with `--host`, `--port`, `--config`, `--verbose`
- [x] CLI: `keycard rooms` to list available environments
- [x] Per-room resource caps applied to containers (memory, CPU, pids)
- [x] Per-room network isolation (`network = "none"`)

### Reliability

- [x] State-polling teardown (no blocking `container.wait()`)
- [x] Grace period with kill fallback on hard disconnect
- [x] Idempotent destroy (safe to call more than once)
- [x] Idle timeout reaper for orphaned sessions
- [x] `--keep` flag: pause room on disconnect, allow reconnect within a window
- [x] Graceful server shutdown (drain active sessions before exit)

### Testing

- [x] Unit tests: auth posture, host key handling, config parsing
- [x] Integration tests: clean exit, dropped connection, unauthorised key, resize
- [x] Auto-skip integration tests when no container runtime is available
- [x] Test timeouts to prevent hangs
- [x] CI integration tests (GitHub Actions Ubuntu runners have Docker)
- [x] Connection stress test (concurrent sessions)

### Packaging

- [x] PyPI release (`pipx install keycard-ssh`)
- [ ] Homebrew formula (`brew install keycard`)
- [x] Versioned releases with changelog
- [x] Man page or `--help` documentation

### Site & Docs

- [x] Landing page (GitHub Pages)
- [x] Favicon, OG card, manifest, 404 page
- [x] README with startup-style positioning
- [x] SECURITY.md with honest threat model
- [x] CONTRIBUTING.md with design principles
- [x] Usage guide with examples
- [x] Architecture overview for contributors

### CI / CD

- [x] Lint, format, types, tests on Python 3.11–3.13
- [x] SHA-pinned GitHub Actions
- [x] Dependabot for actions and pip
- [x] Security scans (Semgrep, Trivy via reusable workflows)
- [x] Integration tests in CI
- [x] Automated PyPI publish on tag

## v2 — Firecracker

Phased roadmap in [FIRECRACKER.md](FIRECRACKER.md). Rooms boot as microVMs
now, and for rooms with no network — which is all of them — guest escape is
in scope. Networking is the one piece left, and it is deferred rather than
merely unstarted.

- [x] Backend interface abstraction (designed from day one, now exercised by a second implementation)
- [x] Rootfs image builds (`rootfs/`, with the guest agent in `guest-agent/`)
- [x] Firecracker microVM provisioning (`--keep` is refused here — a snapshot would write guest RAM to disk in the clear)
- [x] Confining the VMM — Landlock rather than the jailer, which needs root
- [x] Real isolation boundary, claimed in SECURITY.md for rooms with no network, which is every microVM room
- [ ] Tap networking — deliberately deferred, not merely unstarted; both ways of building it cost more security than the feature is currently worth. Reasoning under Phase 3 in [FIRECRACKER.md](FIRECRACKER.md).

## Deliberately out of scope

These are excluded by design, not oversight. Each is defensible individually; together they would turn keycard into a platform, and platforms need configuring.

- Port forwarding
- SFTP / scp
- Multi-user accounts
- Persistent volumes
- Web UI
- Windows host support (client connects from anywhere; server runs on Linux/macOS)
