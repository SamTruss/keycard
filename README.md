# keycard

**Disposable SSH sandboxes. One command, zero setup.**

```
ssh -p 2222 python@yourhost
```

You're now inside a fresh, empty container with Python installed. Install what you like, break what you like. Disconnect, and the whole thing is destroyed.

Your actual machine never knew it happened.

---

## Why

Trying out an unfamiliar script or tool leaves you with two bad options: run it on your own machine and clean up afterwards, or spend ten minutes provisioning a sandbox for a two-minute job. Most people pick the first and regret it.

`docker run -it --rm` gets you most of the way, but it's local-only and verbose. ContainerSSH does the right thing but needs real configuration before first use. Vagrant and multipass are VM-weight. Devcontainers are project-scoped, not ad-hoc.

keycard is the trivial case nobody shipped: **ephemeral SSH sessions with no configuration.**

## How it works

keycard is an SSH server. The username you connect as selects the environment:

| You type | You get |
| --- | --- |
| `ssh python@host` | Fresh container, Python installed |
| `ssh node@host` | Fresh container, Node.js installed |
| `ssh ubuntu@host` | Plain empty Linux box |

On connect, keycard authenticates your public key, spawns a container from the mapped image, allocates a PTY and attaches you. On disconnect, it tears the container down. An idle timeout reaps anything orphaned.

There is no client to install. Your existing `ssh` already speaks the protocol.

### Why a custom server?

OpenSSH can only authenticate usernames that exist as system accounts, so dynamic usernames can't be done with `ForceCommand` or `AuthorizedKeysCommand` tricks. keycard is a purpose-built SSH server (Python + AsyncSSH) rather than a wrapper around OpenSSH. That's precisely what buys the zero-config UX — the username is resolved at auth time instead of being pre-provisioned.

## Status

**Early development.** Not yet released. See [Roadmap](#roadmap).

## Installation

Not yet published. Once released:

```bash
pipx install keycard    # or
brew install keycard
```

For now, from source:

```bash
git clone https://github.com/SamTruss/keycard
cd keycard
pip install -e .
```

Requires Python 3.11+ and a working Docker or Podman daemon.

## Quick start

```bash
# Authorise your key
mkdir -p ~/.config/keycard
cp ~/.ssh/id_ed25519.pub ~/.config/keycard/authorized_keys

# Run the server
keycard serve

# From anywhere
ssh -p 2222 ubuntu@yourhost
```

Tidier, via `~/.ssh/config` on the client:

```
Host sandbox
  HostName yourhost
  Port 2222
  User python
```

...then just `ssh sandbox`.

## Configuration

Config is optional — keycard ships with working defaults. To customise, create `~/.config/keycard/keycard.toml`:

```toml
listen = ":2222"
authorized_keys = "~/.config/keycard/authorized_keys"
idle_timeout = "15m"

[rooms.default]
image = "ubuntu:24.04"

[rooms.python]
image = "python:3.12-slim"
memory = "1g"
cpus = 2

[rooms.node]
image = "node:22-slim"
network = "none"
```

Each `[rooms.*]` key becomes a valid SSH username.

## Where to run it

- **Your own laptop** — works, but you're not gaining much over `docker run`.
- **A homelab box or spare server** — the sweet spot. Your laptop stays untouched; all mess is confined to a machine you don't care about.
- **A cloud VM** — same, plus you can hand the address to other people and each gets their own isolated room. Useful for teaching, workshops, or offering a "try it without installing anything" sandbox for your project.

## Security

> [!IMPORTANT]
> **v1 is a hygiene tool, not a security boundary.**

keycard v1 uses containers, which share a kernel with the host. Container escape is a real class of vulnerability. Do not use v1 to run software you believe to be actively hostile, and do not expose it to untrusted users on a host you care about.

It *is* appropriate for keeping your machine clean from software you broadly trust but don't want permanently installed.

Stronger isolation via Firecracker microVMs is planned for v2. The stronger claim will not be made until it lands.

Current hardening: public-key authentication only (no passwords), no Docker socket passthrough, no host bind mounts by default, dropped capabilities, and optional `network = "none"` per room.

See [SECURITY.md](SECURITY.md) to report a vulnerability.

## Roadmap

**v1 — Docker/Podman backend**
- [ ] SSH server with pubkey auth
- [ ] Username → image resolution
- [ ] PTY allocation, window resize, exit code propagation
- [ ] Teardown on disconnect
- [ ] Idle timeout reaper
- [ ] Per-room resource caps (CPU, memory, pids)
- [ ] `--keep` pause-instead-of-destroy for reconnect windows
- [ ] Homebrew formula + PyPI release

**v2 — Firecracker backend**
- [ ] Backend interface abstraction (design for this from day one)
- [ ] microVM provisioning, rootfs images, tap networking

**Explicitly out of scope for v1:** port forwarding, SFTP/scp, multi-user accounts, persistent volumes, web UI.

## Contributing

Contributions welcome — see [CONTRIBUTING.md](CONTRIBUTING.md). The project is early enough that architectural input is as valuable as code.

## Licence

MIT — see [LICENSE](LICENSE).
