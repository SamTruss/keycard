#!/usr/bin/env python3
"""Smoke-test driver for the agent's dev (TCP) transport.

Not part of the shipped agent. It drives the two ports the way
`FirecrackerBackend` does — data first, then control — over TCP instead of
vsock, so the whole bridge can be exercised on a plain Linux host with no
microVM and no /dev/kvm. See FIRECRACKER.md, Phase 0.

Two scenarios:

  session   one connection: resize, run a command, resize again, exit with a
            specific code, and check the pty output and reported status.
  reattach  two connections: drop the first mid-session and connect again,
            checking the shell is the *same* shell. This is what `--keep`
            depends on — the host snapshots a microVM whose connections have
            gone and reconnects to it later (FIRECRACKER.md, Phase 2).

Usage:
    ./target/debug/keycard-guest-agent --transport tcp --shell /bin/bash &
    python3 test_client.py            # both scenarios
    python3 test_client.py reattach   # just one
"""

from __future__ import annotations

import socket
import sys
import time

HOST = "127.0.0.1"
DATA_PORT = 10000
CTRL_PORT = 10001

# Long enough for a shell to start and echo, short enough that a hang is a
# failure rather than a wait. The agent is local; nothing here is slow.
SETTLE = 0.3
READ_TIMEOUT = 3.0


def connect() -> tuple[socket.socket, socket.socket]:
    """Open both channels in the order the agent accepts them.

    Data first, then control. The agent accepts in that order, so a client
    that reverses it deadlocks both sides — worth doing the same way here as
    in the real backend, since this is the only place it gets exercised.
    """
    data = socket.create_connection((HOST, DATA_PORT), timeout=5)
    ctrl = socket.create_connection((HOST, CTRL_PORT), timeout=5)
    return data, ctrl


def drain(sock: socket.socket, timeout: float = READ_TIMEOUT) -> bytes:
    sock.settimeout(timeout)
    out = b""
    try:
        while True:
            chunk = sock.recv(4096)
            if not chunk:
                break
            out += chunk
    except TimeoutError:
        pass
    return out


def check(label: str, ok: bool, detail: object = "") -> bool:
    print(f"  {'ok  ' if ok else 'FAIL'}  {label}")
    if not ok and detail:
        print(f"        got: {detail!r}")
    return ok


def scenario_session() -> bool:
    """One connection, start to finish."""
    print("session:")
    data, ctrl = connect()

    # Initial size, doubling as the first control message the agent expects.
    ctrl.sendall(b"resize 100 30\n")
    time.sleep(SETTLE)
    data.sendall(b"echo hello-from-pty\n")
    time.sleep(SETTLE)
    ctrl.sendall(b"resize 120 40\n")
    time.sleep(SETTLE)
    data.sendall(b"stty size\n")
    time.sleep(SETTLE)
    data.sendall(b"exit 7\n")

    output = drain(data)
    ctrl_out = drain(ctrl)

    return all(
        [
            check("shell output reaches the data channel", b"hello-from-pty" in output, output),
            check("resize reached the guest pty", b"40 120" in output, output),
            check("exit status came back", ctrl_out.strip().endswith(b"exit 7"), ctrl_out),
        ]
    )


def scenario_reattach() -> bool:
    """Drop a connection mid-session and come back to the same shell."""
    print("reattach:")
    data, ctrl = connect()
    ctrl.sendall(b"resize 100 30\n")
    time.sleep(SETTLE)

    data.sendall(b"MARKER=still-here\n")
    time.sleep(SETTLE)

    # The host goes away without the shell ever exiting — a dropped
    # connection, which under --keep is followed by a snapshot.
    data.close()
    ctrl.close()
    time.sleep(SETTLE)

    data, ctrl = connect()
    ctrl.sendall(b"resize 100 30\n")
    time.sleep(SETTLE)
    data.sendall(b"echo marker-is-$MARKER\n")
    time.sleep(SETTLE)
    output = drain(data)

    same_shell = check(
        "the reattached session is the same shell",
        b"marker-is-still-here" in output,
        output,
    )

    data.sendall(b"exit 3\n")
    ctrl_out = drain(ctrl)
    reports = check(
        "the reattached connection still gets the exit status",
        ctrl_out.strip().endswith(b"exit 3"),
        ctrl_out,
    )
    drain(data)
    return same_shell and reports


SCENARIOS = {"session": scenario_session, "reattach": scenario_reattach}


def main(argv: list[str]) -> int:
    names = argv[1:] or list(SCENARIOS)
    unknown = [name for name in names if name not in SCENARIOS]
    if unknown:
        print(f"unknown scenario(s): {', '.join(unknown)}", file=sys.stderr)
        print(f"available: {', '.join(SCENARIOS)}", file=sys.stderr)
        return 2

    # Order matters when both run: `session` ends by exiting its shell, which
    # leaves the agent ready to start a fresh one for `reattach`.
    ok = True
    for name in names:
        ok = SCENARIOS[name]() and ok

    print("PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
