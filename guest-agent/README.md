# keycard-guest-agent

Phase 0 of `FIRECRACKER.md`. Runs inside a keycard Firecracker room (v2, not
shipped yet): listens on two vsock ports — one carrying raw pty bytes, one
carrying a tiny text control protocol — execs a shell on connect, and
bridges the two until the shell exits.

Not sshd. See `FIRECRACKER.md` for why.

## Building

```bash
cargo build --release
```

Requires a Linux host (pty syscalls and, for the real transport, `AF_VSOCK`
are both Linux-only — this will not build on Windows or macOS).

## Protocol

Two ports, one session at a time — a microVM only ever serves one keycard
connection, so there's no session multiplexing to do (see `session.rs`).

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

`test_client.py` drives both ports directly (connect, send a resize, run a
command, resize again, exit with a specific code) and checks the pty output
and the reported exit status match. It's a throwaway smoke-test script, not
part of the shipped agent.

`--transport vsock` (the default) is what a real microVM guest uses —
untested here, since that needs Phase 2's `FirecrackerBackend` and a real
`/dev/kvm`-capable host to exercise end to end.
