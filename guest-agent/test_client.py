#!/usr/bin/env python3
"""Manual smoke-test driver for Phase 0's dev (TCP) transport.

Not part of the shipped agent — this is a throwaway script used to prove
the vsock-stand-in path actually bridges a shell, honors resize, and
reports exit status, before any of that gets exercised for real inside a
microVM. See FIRECRACKER.md, Phase 0.
"""

import socket
import sys
import time

HOST = "127.0.0.1"
DATA_PORT = 10000
CTRL_PORT = 10001


def main() -> int:
    data = socket.create_connection((HOST, DATA_PORT), timeout=5)
    ctrl = socket.create_connection((HOST, CTRL_PORT), timeout=5)

    # Initial size, doubling as the first control message the agent expects.
    ctrl.sendall(b"resize 100 30\n")

    time.sleep(0.2)
    data.sendall(b"echo hello-from-pty\n")
    time.sleep(0.2)
    ctrl.sendall(b"resize 120 40\n")
    time.sleep(0.2)
    data.sendall(b"stty size\n")
    time.sleep(0.2)
    data.sendall(b"exit 7\n")

    data.settimeout(2)
    output = b""
    try:
        while True:
            chunk = data.recv(4096)
            if not chunk:
                break
            output += chunk
    except TimeoutError:
        pass

    print("---- pty output ----")
    sys.stdout.buffer.write(output)
    print("\n---- pty output end ----")

    ctrl.settimeout(2)
    ctrl_out = b""
    try:
        while True:
            chunk = ctrl.recv(4096)
            if not chunk:
                break
            ctrl_out += chunk
    except TimeoutError:
        pass

    print("ctrl channel said:", ctrl_out)

    ok = b"hello-from-pty" in output and b"40 120" in output and ctrl_out.strip() == b"exit 7"
    print("PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
