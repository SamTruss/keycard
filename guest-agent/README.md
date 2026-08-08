# keycard-guest-agent

Phase 0 of `FIRECRACKER.md`. Runs inside a keycard Firecracker room (v2, not
shipped yet): listens on two vsock ports — one carrying raw pty bytes, one
carrying a tiny text control protocol — execs a shell on connect, and
bridges the two until the shell exits.

Not sshd. See `FIRECRACKER.md` for why.

A connection is not a session. The shell belongs to the agent, not to
whichever host connection happens to be attached, so a host that drops away
— and, under `--keep`, snapshots the whole microVM before coming back — is
reattached to the same shell rather than given a fresh one. A shell that has
actually exited ends its session for good; the next connection starts a new
one.

## Building

```bash
cargo build --release
```

Requires a Linux host (pty syscalls and, for the real transport, `AF_VSOCK`
are both Linux-only — this will not build on Windows or macOS).

## Protocol

Two ports, one session at a time — a microVM only ever serves one keycard
connection, so there's no session multiplexing to do (see `session.rs`). The
host must connect to the **data port first and the control port second**; the
agent accepts in that order, so reversing it deadlocks both sides.

- **data port**: raw bytes, straight to/from the pty. No framing.
- **control port**: newline-delimited text.
  - `resize <cols> <rows>` — host to guest, applied via `TIOCSWINSZ`. The
    first line sent doubles as the initial size; there's no separate open
    message.
  - `exit <code>` — guest to host, sent once, right before both
    connections close.

## Testing without a microVM

`--transport tcp` swaps `AF_VSOCK` for plain TCP loopback, so the whole
bridge can be exercised on a bare Linux host — no Firecracker, no `/dev/kvm`
required:

```bash
cargo build
./target/debug/keycard-guest-agent --transport tcp --shell /bin/bash &
python3 test_client.py
```

`test_client.py` drives both ports directly and runs two scenarios:

- **session** — connect, send a resize, run a command, resize again, exit
  with a specific code; check the pty output and reported status match.
- **reattach** — drop the connection mid-session, connect again, and check
  the shell is the same one. This is what `--keep` rests on, and it is the
  only part of Phase 2 that can be verified without a microVM.

Both run in CI on every push, so the bridge cannot bitrot unnoticed. It's a
smoke-test script, not part of the shipped agent.

`--transport vsock` (the default) is what a real microVM guest uses —
untested here, since that needs a real `/dev/kvm`-capable host to exercise
end to end. The session logic underneath is transport-agnostic and shared,
so what the TCP path proves about the bridge holds for vsock too; what it
cannot prove is the vsock plumbing itself.
