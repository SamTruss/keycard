"""Host side of Firecracker's vsock multiplexer.

A microVM's vsock device is exposed to the host as a single UNIX socket. To
reach a port inside the guest you connect to that socket and send a one-line
``CONNECT <port>`` handshake; Firecracker answers ``OK <port>`` and from then
on the connection is a plain byte pipe to whatever is listening in the guest.

That pipe is what carries a keycard room: the guest agent's data port (raw
pty bytes) and control port (resize in, exit status out). See
FIRECRACKER.md, Phase 2, and ``guest-agent/``.
"""

from __future__ import annotations

import asyncio
import logging
import socket
import time
from pathlib import Path

log = logging.getLogger(__name__)

# Firecracker's reply is a short line, so a bounded read is a bug guard, not
# a real limit — anything longer means we are not talking to a vsock device.
_MAX_ACK = 64

# Gap between handshake attempts while the guest is still booting. The guest
# agent binds its ports within milliseconds of init running, but the kernel
# boot in front of that is what actually sets the pace.
_RETRY_DELAY = 0.05


class VsockError(RuntimeError):
    """The CONNECT handshake did not produce a usable pipe."""


def parse_ack(line: bytes) -> int | None:
    """Read Firecracker's handshake reply.

    Returns the port number it echoed back, or None if the line is not an
    acknowledgement at all. The number is only logged, never checked against
    the port we asked for: the documented reply names the *host-side* port
    Firecracker assigned, which is not required to match the guest port in
    the request, and asserting they are equal would be inventing a contract.
    """
    if not line.startswith(b"OK"):
        return None
    _, _, rest = line.partition(b" ")
    try:
        return int(rest.strip())
    except ValueError:
        return None


async def _read_ack(loop: asyncio.AbstractEventLoop, sock: socket.socket) -> bytes:
    """Read the acknowledgement line, one byte at a time.

    Deliberately unbuffered. Anything that reads ahead — a StreamReader, a
    plain recv() of a sensible size — will happily pull in session bytes that
    arrived behind the newline, and those bytes are the first thing the shell
    ever said. They are gone by the time anyone notices.
    """
    line = bytearray()
    while len(line) < _MAX_ACK:
        byte = await loop.sock_recv(sock, 1)
        if not byte:
            break
        if byte == b"\n":
            return bytes(line)
        line += byte
    return bytes(line)


async def connect(uds_path: Path, port: int) -> socket.socket:
    """Open one connection to *port* inside the guest.

    Raises VsockError if the guest has nothing listening there — Firecracker
    signals that by dropping the connection rather than by answering.
    """
    loop = asyncio.get_running_loop()
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.setblocking(False)
    try:
        await loop.sock_connect(sock, str(uds_path))
        await loop.sock_sendall(sock, f"CONNECT {port}\n".encode())
        ack = await _read_ack(loop, sock)
    except OSError as exc:
        sock.close()
        raise VsockError(f"vsock connect to guest port {port} failed: {exc}") from exc
    except BaseException:
        sock.close()
        raise

    assigned = parse_ack(ack)
    if assigned is None:
        sock.close()
        raise VsockError(f"no listener on guest port {port} (handshake said {bytes(ack)!r})")
    log.debug("vsock connected: guest port %d (host side %d)", port, assigned)
    return sock


async def connect_with_retry(
    uds_path: Path,
    port: int,
    timeout: float,  # noqa: ASYNC109 - a retry budget, not a cancellation deadline
) -> socket.socket:
    """`connect`, retried until the guest answers or *timeout* runs out.

    This is the boot wait. Between InstanceStart and the agent's first
    accept() there is a kernel to boot and a rootfs init to run, and every
    attempt in that window fails the same way one against a broken image
    would — so the only difference between "still booting" and "never going
    to boot" is how long we have been at it.
    """
    deadline = time.monotonic() + timeout
    attempts = 0
    while True:
        attempts += 1
        try:
            sock = await connect(uds_path, port)
        except VsockError as exc:
            if time.monotonic() >= deadline:
                raise VsockError(
                    f"guest did not answer on port {port} within {timeout:.0f}s "
                    f"({attempts} attempts): {exc}"
                ) from exc
            await asyncio.sleep(_RETRY_DELAY)
            continue
        if attempts > 1:
            log.debug("guest answered on port %d after %d attempts", port, attempts)
        return sock
