"""The hand-rolled HTTP-over-UNIX-socket client for Firecracker's API.

The wire format is the part most likely to be quietly wrong, so it is tested
on its own — no firecracker, no /dev/kvm. The round-trip tests below run
against a plain asyncio UNIX server that answers like Firecracker does.
"""

from __future__ import annotations

import asyncio
import socket
from pathlib import Path

import pytest

from keycard.backends import fcapi

pytestmark_unix = pytest.mark.skipif(
    not hasattr(socket, "AF_UNIX"), reason="needs AF_UNIX (Linux/macOS)"
)


# -- encoding ---------------------------------------------------------------


def test_request_has_a_body_and_the_length_to_match() -> None:
    raw = fcapi.encode_request("PUT", "/machine-config", {"vcpu_count": 2})
    head, _, body = raw.partition(b"\r\n\r\n")
    assert head.startswith(b"PUT /machine-config HTTP/1.1\r\n")
    assert body == b'{"vcpu_count": 2}'
    assert f"Content-Length: {len(body)}".encode() in head
    assert b"Content-Type: application/json" in head


def test_bodyless_request_still_declares_zero_length() -> None:
    # Firecracker's parser wants a Content-Length even when there is nothing
    # to send; omitting it makes it wait for a body that never arrives.
    raw = fcapi.encode_request("GET", "/")
    head, _, body = raw.partition(b"\r\n\r\n")
    assert body == b""
    assert b"Content-Length: 0" in head
    assert b"Content-Type" not in head


def test_every_request_carries_a_host_header() -> None:
    assert b"Host: localhost" in fcapi.encode_request("PUT", "/actions", {"a": 1})


# -- head parsing -----------------------------------------------------------


def test_parse_head_lowercases_header_names() -> None:
    status, headers = fcapi.parse_head(
        b"HTTP/1.1 200 OK\r\nContent-Length: 12\r\nServer: Firecracker API\r\n\r\n"
    )
    assert status == 200
    assert headers["content-length"] == "12"
    assert headers["server"] == "Firecracker API"


def test_parse_head_handles_a_reason_free_status_line() -> None:
    status, _ = fcapi.parse_head(b"HTTP/1.1 204\r\n\r\n")
    assert status == 204


@pytest.mark.parametrize("head", [b"not http at all\r\n\r\n", b"HTTP/1.1 nope\r\n\r\n"])
def test_parse_head_rejects_junk(head: bytes) -> None:
    with pytest.raises(ValueError):
        fcapi.parse_head(head)


def test_fault_message_is_pulled_out_of_an_error_body() -> None:
    body = b'{"fault_message":"Invalid vCPU count"}'
    assert fcapi.fault_message(body) == "Invalid vCPU count"


@pytest.mark.parametrize("body", [b"", b"not json", b"[]", b'{"other":1}', b'{"fault_message":3}'])
def test_fault_message_never_raises_on_a_body_it_does_not_recognise(body: bytes) -> None:
    # It runs while reporting some other failure; raising here would replace
    # the real error with a parsing one.
    assert fcapi.fault_message(body) == ""


def test_api_error_message_includes_the_fault() -> None:
    err = fcapi.ApiError("PUT", "/drives/rootfs", 400, b'{"fault_message":"no such file"}')
    assert "PUT /drives/rootfs" in str(err)
    assert "400" in str(err)
    assert "no such file" in str(err)


# -- round trip -------------------------------------------------------------


def _content_length(head: bytes) -> int:
    for line in head.decode().split("\r\n"):
        name, _, value = line.partition(":")
        if name.strip().lower() == "content-length":
            return int(value.strip())
    return 0


class FakeFirecracker:
    """An asyncio UNIX server that answers the way Firecracker does."""

    def __init__(self, status: int = 204, body: bytes = b"") -> None:
        self.status = status
        self.body = body
        self.requests: list[bytes] = []
        self._server: asyncio.AbstractServer | None = None

    async def start(self, path: Path) -> None:
        self._server = await asyncio.start_unix_server(self._handle, str(path))

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        head = await reader.readuntil(b"\r\n\r\n")
        length = _content_length(head)
        body = await reader.readexactly(length) if length else b""
        self.requests.append(head + body)
        writer.write(
            f"HTTP/1.1 {self.status} X\r\nContent-Length: {len(self.body)}\r\n\r\n".encode()
            + self.body
        )
        await writer.drain()
        writer.close()


@pytestmark_unix
async def test_successful_call_round_trips(tmp_path: Path) -> None:
    server = FakeFirecracker(status=204)
    sock = tmp_path / "api.sock"
    await server.start(sock)
    try:
        api = fcapi.FirecrackerApi(sock)
        response = await api.put("/actions", {"action_type": "InstanceStart"})
    finally:
        await server.stop()

    assert response.status == 204
    assert b'{"action_type": "InstanceStart"}' in server.requests[0]


@pytestmark_unix
async def test_error_status_becomes_an_api_error(tmp_path: Path) -> None:
    server = FakeFirecracker(status=400, body=b'{"fault_message":"bad drive"}')
    sock = tmp_path / "api.sock"
    await server.start(sock)
    try:
        api = fcapi.FirecrackerApi(sock)
        with pytest.raises(fcapi.ApiError, match="bad drive") as caught:
            await api.put("/drives/rootfs", {"drive_id": "rootfs"})
    finally:
        await server.stop()

    assert caught.value.status == 400


@pytestmark_unix
async def test_wait_ready_returns_once_the_socket_accepts(tmp_path: Path) -> None:
    server = FakeFirecracker()
    sock = tmp_path / "api.sock"
    api = fcapi.FirecrackerApi(sock)

    async def start_late() -> None:
        await asyncio.sleep(0.05)
        await server.start(sock)

    starter = asyncio.create_task(start_late())
    try:
        await api.wait_ready(timeout=5.0)
    finally:
        await starter
        await server.stop()


@pytestmark_unix
async def test_wait_ready_gives_up(tmp_path: Path) -> None:
    api = fcapi.FirecrackerApi(tmp_path / "never.sock")
    with pytest.raises(TimeoutError):
        await api.wait_ready(timeout=0.1)


@pytestmark_unix
async def test_chunked_response_is_refused_rather_than_misread(tmp_path: Path) -> None:
    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await reader.readuntil(b"\r\n\r\n")
        writer.write(b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n0\r\n\r\n")
        await writer.drain()
        writer.close()

    sock = tmp_path / "api.sock"
    server = await asyncio.start_unix_server(handle, str(sock))
    try:
        with pytest.raises(ValueError, match="transfer-encoding"):
            await fcapi.FirecrackerApi(sock).put("/x")
    finally:
        server.close()
        await server.wait_closed()
