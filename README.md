<div align="center">

# keycard

### Disposable machines, on demand.

**One command. Zero setup. Gone when you're done.**

[![CI](https://github.com/SamTruss/keycard/actions/workflows/ci.yml/badge.svg)](https://github.com/SamTruss/keycard/actions)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://python.org)
[![Status: pre-alpha](https://img.shields.io/badge/status-pre--alpha-orange.svg)](#status)

</div>

---

```console
$ ssh python@yourhost

root@a4f9c2 :~#
```

You're inside a brand new machine. Python's installed. Nothing else is.

Install what you like. Break what you like.

Then close the window — and it never existed.

---

## The problem

You want to try something. A script a colleague sent. A tool you're not sure about. A dependency you'd rather not commit to.

Today you pick between two bad options:

**Run it locally.** It works. It also leaves files, changes settings, and installs things you didn't ask for. Your machine is permanently a little bit dirtier.

**Provision a sandbox first.** Safe. Also ten minutes of setup for a two-minute job — so you skip it, and do the first thing.

There's no third option. There should be.

## The fix

```
ssh <what-you-need>@yourhost
```

That's the whole interface. The word before the `@` is the request.

| Command | What you get |
| :-- | :-- |
| `ssh ubuntu@host` | A clean Linux box |
| `ssh python@host` | Python 3.12, nothing else |
| `ssh node@host` | Node 22, nothing else |
| `ssh offline@host` | Clean box, no network |

Disconnect and it's destroyed. No cleanup. No leftovers. No cron job to reap it.

## Why it feels like magic

**Nothing to install.** No client, no CLI, no plugin. `ssh` is already on your machine and already speaks the protocol. keycard runs on the other end and does all the work.

**Nothing to configure.** Sensible defaults out of the box. The config file exists, but it's opt-in. If setup were required, the whole point would be gone.

**Nothing to remember.** Not a flag, not a subcommand, not a wizard. Just the name of what you want.

> Under the hood, OpenSSH can only let in usernames that already exist as system accounts — so dynamic usernames aren't possible with config tricks. keycard is a purpose-built SSH server. That's exactly what buys the zero-config experience: your request is resolved at auth time instead of provisioned in advance.

## Who it's for

**Developers** who want to try things without sanding down their laptop.

**Teachers and workshop runners** handing thirty people an identical clean environment with a single line on a slide.

**Maintainers** who want a *"try it without installing anything"* link in their README.

## Status

> [!WARNING]
> **Pre-alpha.** Not released, not usable yet. This repo is scaffolding and intent.

Building in the open. [Roadmap below](#roadmap) — issues and architectural pushback welcome while everything's still cheap to change.

## Getting started

Once released:

```bash
pipx install keycard
```
```bash
brew install keycard
```

From source, today:

```bash
git clone https://github.com/SamTruss/keycard && cd keycard
pip install -e .
```

Then:

```bash
# Your key is your keycard
mkdir -p ~/.config/keycard
cp ~/.ssh/id_ed25519.pub ~/.config/keycard/authorized_keys

keycard serve
```

Requires Python 3.11+ and Docker or Podman.

<details>
<summary><b>Making it one word</b></summary>

Port 22 belongs to your system sshd, so keycard listens on 2222. Bury it in `~/.ssh/config` on the client:

```
Host sandbox
  HostName yourhost
  Port 2222
  User python
```

Now it's just `ssh sandbox`.
</details>

<details>
<summary><b>Adding your own rooms</b></summary>

`~/.config/keycard/keycard.toml` — optional, defaults work fine.

```toml
listen = ":2222"
idle_timeout = "15m"

[rooms.python]
image = "python:3.12-slim"
memory = "1g"
cpus = 2

[rooms.offline]
image = "ubuntu:24.04"
network = "none"
```

Every `[rooms.*]` key becomes a valid SSH username.
</details>

<details>
<summary><b>Where to run it</b></summary>

**Your laptop** — works, but you're not gaining much over `docker run`.

**A homelab box** — the sweet spot. Your laptop stays pristine; the mess lives somewhere you don't care about.

**A cloud VM** — same, but you can hand out the address and everyone gets their own room. This is where it stops being a personal convenience.
</details>

## Straight talk on security

> [!IMPORTANT]
> **v1 keeps your machine clean. It does not keep you safe from hostile code.**

Containers share a kernel with the host, and escapes are a real, recurring class of bug. keycard can't fix that at the container layer, so it won't pretend to.

**Use it for:** software you broadly trust but don't want permanently installed.

**Don't use it for:** anything you actually believe is malicious, or handing shells to people you don't trust on a host you care about.

Firecracker microVMs are planned for v2, and that's when the stronger claim gets made — not before. Overstating this would be the easiest way to get someone hurt.

Shipping today: pubkey auth only, no Docker socket passthrough, no host mounts, dropped capabilities, optional per-room network isolation. Full threat model in [SECURITY.md](SECURITY.md).

## Roadmap

**v1 — Docker / Podman**

- [ ] SSH server, pubkey auth
- [ ] Username → image resolution
- [ ] PTY, resize, exit codes
- [ ] Teardown on disconnect
- [ ] Idle reaper
- [ ] Per-room CPU / memory / pid caps
- [ ] `--keep` for reconnect windows
- [ ] PyPI + Homebrew

**v2 — Firecracker**

- [ ] Backend abstraction
- [ ] microVM provisioning, rootfs, tap networking

**Deliberately not building:** port forwarding, SFTP, persistent volumes, multi-user accounts, web UI. Each is defensible alone; together they'd make keycard a platform, and platforms need configuring.

## Contributing

Early enough that opinions are worth as much as commits. [CONTRIBUTING.md](CONTRIBUTING.md).

<div align="center">

---

MIT © [Sam Truss](https://github.com/SamTruss)

</div>
