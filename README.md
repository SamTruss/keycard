<div align="center">
<br>

# keycard

**Disposable machines, on demand.**

<sub>`ᴄʜᴇᴄᴋ ɪɴ` · `ᴍᴀᴋᴇ ᴀ ᴍᴇss` · `ᴄʜᴇᴄᴋ ᴏᴜᴛ`</sub>

<br>

[![status](https://img.shields.io/badge/status-pre--alpha-C9A227?style=flat-square&labelColor=1D1A26)](#status)
[![license](https://img.shields.io/badge/license-MIT-8B8397?style=flat-square&labelColor=1D1A26)](LICENSE)
[![python](https://img.shields.io/badge/python-3.11+-8B8397?style=flat-square&labelColor=1D1A26)](pyproject.toml)
[![backend](https://img.shields.io/badge/docker%20%7C%20podman-8B8397?style=flat-square&labelColor=1D1A26)](#configuration)

<br>

</div>

```console
$ ssh -p 2222 python@yourhost

keycard: key accepted · room 101
keycard: container up · pty attached

root@a4f9c2:~# _
```

<div align="center">
<sub>A brand new machine. Python installed, nothing else.<br>
Install what you like. Break what you like. Close the window — and it never existed.</sub>
</div>

<br>

---

## The problem

You want to try something. A script a colleague sent. A tool you're unsure about. A dependency you'd rather not commit to.

Today you pick between two bad options:

**Run it locally.** It works. It also leaves files, changes settings, and installs things you didn't ask for. Your machine is permanently a little bit dirtier.

**Provision a sandbox first.** Safe. Also ten minutes of setup for a two-minute job — so you skip it, and do the first thing.

There should be a third option.

---

## The fix

The word before the `@` is the request. That's the whole interface.

| | |
|:--|:--|
| `ssh ubuntu@host` | A clean Linux box |
| `ssh python@host` | Python 3.12, nothing else |
| `ssh node@host` | Node 22, nothing else |
| `ssh offline@host` | Clean box, no network |

Disconnect and it's destroyed. No cleanup. No leftovers. No cron job to reap it.

---

## Why it feels like magic

**Nothing to install.** No client, no CLI, no plugin. `ssh` is already on your machine and already speaks the protocol. keycard runs on the other end and does the work.

**Nothing to configure.** Sensible defaults out of the box. The config file exists, but it's opt-in. If setup were required, the whole point would be gone.

**Nothing to remember.** Not a flag, not a subcommand, not a wizard. Just the name of what you want.

> [!NOTE]
> OpenSSH can only admit usernames that exist as system accounts, so dynamic usernames aren't possible via `ForceCommand` or `AuthorizedKeysCommand` tricks. keycard is a purpose-built SSH server. That's exactly what buys the zero-config experience — your request is resolved at auth time instead of provisioned in advance.

---

## The rooms

<table>
<tr>
<td width="50%" valign="top">

**`RM 101`** &nbsp;·&nbsp; Ask by name

Whatever you type before the `@` is the environment you get. No flags, no subcommands, no wizard.

```
ssh ubuntu@host  → clean Linux
ssh python@host  → Python 3.12
ssh node@host    → Node 22
```

</td>
<td width="50%" valign="top">

**`RM 102`** &nbsp;·&nbsp; Housekeeping is automatic

Disconnect and the room is destroyed. Drop the connection instead and an idle timer catches it.

```
disconnect  → destroyed
15m idle    → reaped
--keep      → paused, not gone
```

</td>
</tr>
<tr>
<td width="50%" valign="top">

**`RM 103`** &nbsp;·&nbsp; You already have the client

Nothing to install where you're sitting. `ssh` shipped with your OS. keycard is what answers.

```
Host sandbox
  HostName yourhost
  Port 2222
  User python
```

</td>
<td width="50%" valign="top">

**`RM 104`** &nbsp;·&nbsp; Hand out the address

Run it on a box you don't care about and everyone who connects gets their own room.

```
ana@host   → rm 1 · isolated
ben@host   → rm 2 · isolated
cara@host  → rm 3 · isolated
```

</td>
</tr>
</table>

**`RM 105`** &nbsp;·&nbsp; Under renovation

v1 runs on containers, which is enough to keep your machine clean. Real isolation needs Firecracker microVMs, and that's what v2 is for. It isn't built yet, so this page won't pretend otherwise — see the [roadmap](#roadmap).

---

## Status

> [!WARNING]
> **Pre-alpha.** Not released, not usable yet. This repo is scaffolding and intent.

Building in the open. Issues and architectural pushback welcome while everything's still cheap to change.

---

## Getting started

Once released:

```bash
pipx install keycard    # or: brew install keycard
```

From source, today:

```bash
git clone https://github.com/SamTruss/keycard && cd keycard
pip install -e .

# Your key is your keycard
mkdir -p ~/.config/keycard
cp ~/.ssh/id_ed25519.pub ~/.config/keycard/authorized_keys

keycard serve
```

Requires Python 3.11+ and Docker or Podman.

<details>
<summary><b>Making it one word</b></summary>

<br>

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

<br>

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

<br>

**Your laptop** — works, but you're not gaining much over `docker run`.

**A homelab box** — the sweet spot. Your laptop stays pristine; the mess lives somewhere you don't care about.

**A cloud VM** — same, but you can hand out the address and everyone gets their own room. This is where it stops being a personal convenience.

</details>

---

## Straight talk on security

> [!IMPORTANT]
> **v1 keeps your machine clean. It does not keep you safe from hostile code.**

Containers share a kernel with the host, and escapes are a real, recurring class of bug. keycard can't fix that at the container layer, so it won't claim to.

| | |
|:--|:--|
| **Good for** | Software you broadly trust but don't want permanently installed. Throwaway tests. Teaching. Clean-room reproductions. |
| **Not for** | Anything you actually believe is malicious, or handing shells to people you don't trust on a host you care about. |

Shipping today: public-key auth only, no Docker socket passthrough, no host mounts, dropped capabilities, optional per-room network isolation. Full threat model in [SECURITY.md](SECURITY.md).

---

## Roadmap

**v1 — Docker / Podman**

- [x] SSH server, pubkey auth
- [x] Username → image resolution
- [x] PTY, resize, exit codes
- [x] Teardown on disconnect
- [x] Idle reaper
- [x] Per-room CPU / memory / pid caps
- [x] `--keep` for reconnect windows
- [ ] PyPI + Homebrew

**v2 — Firecracker**

- [ ] Backend abstraction
- [ ] microVM provisioning, rootfs, tap networking

**Deliberately not building:** port forwarding, SFTP, persistent volumes, multi-user accounts, web UI. Each is defensible alone; together they'd make keycard a platform, and platforms need configuring.

---

## Contributing

Early enough that opinions are worth as much as commits. See [CONTRIBUTING.md](CONTRIBUTING.md). Notable changes are tracked in [CHANGELOG.md](CHANGELOG.md).

<div align="center">
<br>
<sub>

MIT © [Sam Truss](https://github.com/SamTruss) &nbsp;·&nbsp; v0.0.1 &nbsp;·&nbsp; built in the terminal

</sub>
</div>
