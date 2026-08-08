"""The host side of Firecracker's vsock handshake.

Exercised against a UNIX socketpair rather than a microVM: the handshake is
plain bytes on a stream, and every bug it can have — misparsing the reply,
reading past the newline — shows up there just as well.
"""

from __future__ import annotations

import asyncio
import socket
from pathlib import Path

import pytest

from keycard.backends import vsock

pytestmark = pytest.mark.skipif(
    not hasattr(socket, "AF_UNIX"), reason="needs AF_UNIX (Linux/macOS)"
)


def test_parse_ack_reads_the_port() -> None:
    assert vsock.parse_ack(b"OK 10000") == 10000


def test_parse_ack_does_not_require_the_port_to_match_the_request() -> None:
    # Firecracker's documented reply names the host-side port it assigned,
    # which need not be the guest port we asked for. Asserting equality would
    # be inventing a contract we cannot check.
    assert vsock.parse_ack(b"OK 49152") == 49152


@pytest.mark.parametrize("line", [b"", b"ERR", b"NOPE 1", b"OK", b"OK notaport"])
def test_parse_ack_rejects_anything_that_is_not_an_acknowledgement(line: bytes) -> None:
    assert vsock.parse_ack(line) is None


class FakeVsock:
    """A UNIX server standing in for a microVM's vsock device."""

    def __init__(self, reply: bytes | None, trailing: bytes = b"") -> None:
        self.reply = reply
        self.trailing = trailing
        self.requests: list[bytes] = []
        self._server: asyncio.AbstractServer | None = None

    async def start(self, path: Path) -> None:
        self._server = await asyncio.start_unix_server(self._handle, str(path))

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.requests.append(await reader.readline())
        if self.reply is None:
            # How Firecracker signals "nothing is listening in there".
            writer.close()
            return
        writer.write(self.reply + self.trailing)
        await writer.drain()


async def test_connect_sends_the_handshake_and_returns_the_pipe(tmp_path: Path) -> None:
    server = FakeVsock(b"OK 10000\n")
    path = tmp_path / "vsock.sock"
    await server.start(path)
    try:
        sock = await vsock.connect(path, 10000)
        sock.close()
    finally:
        await server.stop()

    assert server.requests == [b"CONNECT 10000\n"]


async def test_session_bytes_behind_the_newline_survive(tmp_path: Path) -> None:
    """The reason the acknowledgement is read one byte at a time.

    A buffered read would swallow whatever arrived in the same packet as the
    reply — and that is the shell's first output, the thing the user sees
    before anything else.
    """
    server = FakeVsock(b"OK 10000\n", trailing=b"root@keycard:~# ")
    path = tmp_path / "vsock.sock"
    await server.start(path)
    try:
        sock = await vsock.connect(path, 10000)
        loop = asyncio.get_running_loop()
        assert await loop.sock_recv(sock, 64) == b"root@keycard:~# "
        sock.close()
    finally:
        await server.stop()


async def test_no_listener_in_the_guest_is_an_error(tmp_path: Path) -> None:
    server = FakeVsock(None)
    path = tmp_path / "vsock.sock"
    await server.start(path)
    try:
        with pytest.raises(vsock.VsockError, match="no listener on guest port"):
            await vsock.connect(path, 10001)
    finally:
        await server.stop()


async def test_missing_socket_is_an_error_not_a_hang(tmp_path: Path) -> None:
    with pytest.raises(vsock.VsockError, match="failed"):
        await vsock.connect(tmp_path / "nothing.sock", 10000)


async def test_retry_waits_for_the_guest_to_finish_booting(tmp_path: Path) -> None:
    # Stands in for the window between InstanceStart and the agent's first
    # accept(): the socket is there, but nothing answers on it yet.
    path = tmp_path / "vsock.sock"
    booting = FakeVsock(None)
    await booting.start(path)
    booted = FakeVsock(b"OK 10000\n")

    async def finish_booting() -> None:
        await asyncio.sleep(0.15)
        await booting.stop()
        path.unlink(missing_ok=True)
        await booted.start(path)

    finisher = asyncio.create_task(finish_booting())
    try:
        sock = await vsock.connect_with_retry(path, 10000, timeout=5.0)
        sock.close()
    finally:
        await finisher
        await booted.stop()

    assert booted.requests == [b"CONNECT 10000\n"]


async def test_retry_eventually_gives_up(tmp_path: Path) -> None:
    server = FakeVsock(None)
    path = tmp_path / "vsock.sock"
    await server.start(path)
    try:
        with pytest.raises(vsock.VsockError, match="did not answer"):
            await vsock.connect_with_retry(path, 10000, timeout=0.2)
    finally:
        await server.stop()
