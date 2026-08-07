# Usage guide

Everything below assumes you've read the [README](README.md) pitch and want
the actual how-to. For the full config reference, see
[keycard.example.toml](keycard.example.toml); for a security read before you
expose this anywhere, see [SECURITY.md](SECURITY.md).

## Prerequisites

- Python 3.11+
- A running Docker or Podman daemon, reachable the same way `docker` (or
  `podman`) the CLI reaches it
- An SSH key pair on the machine you'll connect *from* (`ssh-keygen -t
  ed25519` if you don't have one)

## Install

Not on PyPI yet (see [SCOPE.md](SCOPE.md)) — install from source:

```bash
git clone https://github.com/SamTruss/keycard
cd keycard
pip install -e .
```

## Quick start

keycard needs one thing before it will start: a list of public keys allowed
to connect, in standard OpenSSH `authorized_keys` format.

```bash
mkdir -p ~/.config/keycard
cp ~/.ssh/id_ed25519.pub ~/.config/keycard/authorized_keys

keycard up
```

That's it. No config file required — `ubuntu`, `python`, and `node` rooms
exist out of the box. On first run keycard also generates its own SSH host
key at `~/.config/keycard/host_key` (0600, kept forever — regenerating it
would break host-key verification for every client that's connected before).

From another terminal, or another machine entirely:

```bash
ssh -p 2222 python@yourhost
```

The username picks the room. Disconnect (or type `exit`) and the container
is gone.

<details>
<summary>Making it one word</summary>

Port 22 usually belongs to your system's own sshd, so keycard listens on
2222 by default. Put the details in `~/.ssh/config` on the client:

```
Host sandbox
  HostName yourhost
  Port 2222
  User python
```

Now it's just `ssh sandbox`.

</details>

## Configuring rooms

`~/.config/keycard/keycard.toml` is entirely optional — copy
[keycard.example.toml](keycard.example.toml) as a starting point, or write
your own:

```toml
listen = ":2222"
idle_timeout = "15m"
shutdown_grace = "30s"
keep_window = "0"

[rooms.python]
image = "python:3.12-slim"
memory = "1g"
cpus = 2

[rooms.offline]
image = "ubuntu:24.04"
network = "none"
```

Every key under `[rooms.*]` becomes a valid SSH username — `ssh
offline@host` above gets a room with no outbound network. If the file
defines no `[rooms.*]` at all, keycard falls back to the three built-in
rooms rather than starting with nothing.

Per-room knobs, all optional:

| Key | Effect |
|---|---|
| `image` | Required. Any image the daemon can pull or already has. |
| `memory` | Hard memory cap, e.g. `"512m"`, `"2g"`. Default `1g`. |
| `cpus` | CPU limit as a core count, e.g. `2`. Default uncapped. |
| `pids_limit` | Max processes/threads in the container. Default `512`. |
| `network` | `"none"` disables outbound networking entirely. |

## Command reference

Full detail: `keycard --help`, `keycard up --help`, or `man ./man/keycard.1`
from a repo checkout (not yet installed to your system's man path — that
lands with packaging, see [SCOPE.md](SCOPE.md)). The short version:

```bash
keycard up                              # run the server with defaults
keycard up --host 0.0.0.0 --port 2222   # bind explicitly
keycard up --config ./keycard.toml      # use a specific config file
keycard up -v                           # debug logging
keycard up --keep 10m                   # see below
keycard rooms                           # list what the config would offer
```

## Recipes

**Tune the idle reaper.** A connection that never sends a clean FIN (dead
wifi, a suspended laptop) leaves a room running until `idle_timeout` of
silence passes, then it's destroyed and the client sees exit status `124` —
the same convention GNU `timeout(1)` uses.

```toml
idle_timeout = "5m"   # tighter than the 15m default
idle_timeout = "0"    # disable the reaper entirely
```

**Survive a dropped connection.** `--keep` (or `keep_window` in config)
pauses a room instead of destroying it when the connection drops *while the
shell is still running* — a typed `exit` still destroys immediately, since
that's a deliberate goodbye, not a disconnect. Reconnect under the same
username within the window and you're back where you left off, background
jobs included; miss the window and it's destroyed like normal.

```bash
keycard up --keep 10m
```

```console
$ ssh -p 2222 python@yourhost
[... connection drops — dead wifi, closed laptop lid ...]

$ ssh -p 2222 python@yourhost      # reconnect within 10 minutes

KEYCARD RESUMED
welcome back to python (python:3.12-slim)
```

**Shut down without dropping people mid-command.** `shutdown_grace`
controls how long `keycard up` waits, after Ctrl-C/SIGTERM, for connected
sessions to finish on their own before force-closing them. `0` cuts
everyone off immediately, matching `docker stop -t 0`.

```toml
shutdown_grace = "1m"
```

```console
keycard: server shutting down, 60s to finish up
```
*(written to every connected session the moment shutdown starts)*

## Troubleshooting

**`no authorized_keys at ...`** — keycard refuses to start without one. The
error message includes the exact `mkdir`/`cp` commands to fix it; see Quick
start above.

**`Permission denied (publickey)` from the client** — the connecting key
isn't in `~/.config/keycard/authorized_keys` on the server. Password auth
isn't implemented and never will be (see SECURITY.md).

**`keycard: no room available`** — either the daemon isn't reachable
(check `docker ps` works from the account running keycard), or something
went wrong pulling/starting the image; check the server's own log output,
which prints the real exception.

**Host key warning after reinstalling the server** — expected if
`~/.config/keycard/host_key` was deleted or moved; every existing client
now sees a mismatch. Removing the old entry from the client's
`known_hosts` is the only fix; there's no way to avoid this once the key
changes.

**Nothing happens on `keycard up`** — port 2222 (or whatever you
configured) may already be in use, or you may need `sudo`/group membership
to reach the Docker socket. Run with `-v` for debug logging.

## Where things live

| Path | What |
|---|---|
| `~/.config/keycard/authorized_keys` | Permitted public keys (OpenSSH format) |
| `~/.config/keycard/host_key` | Server's ed25519 host key, generated once |
| `~/.config/keycard/keycard.toml` | Optional config; see above |

All three paths can be overridden — `authorized_keys` and `host_key` from
inside `keycard.toml`, or the config file's own location via `--config`.
