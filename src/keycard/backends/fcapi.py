"""Firecracker's control API, spoken over its UNIX socket.

Firecracker configures a microVM through HTTP/1.1 on a UNIX domain socket:
about six PUTs and a PATCH, each with a small JSON body and an empty
response. That is a long way short of a whole HTTP client — no redirects, no
chunked bodies, no TLS, no connection pool, no retries — so this hand-rolls
the wire format rather than adding an async HTTP dependency to a project
whose pitch is that it has almost none.

Request encoding and response-head parsing are pure functions on purpose:
they are the part most likely to be subtly wrong, and this way they are
testable on any machine, with no socket and certainly no ``/dev/kvm``.

See FIRECRACKER.md, Phase 2.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

# Firecracker ignores the Host header, but HTTP/1.1 requires one and its
# parser is entitled to reject a request without it.
HOST_HEADER = "localhost"

# Generous: these calls are local, synchronous inside firecracker, and never
# do I/O of their own beyond opening the files they were handed. Anything
# slower than this is a hang, not a slow call — except snapshot creation,
# which writes the whole guest memory out and gets its own budget.
REQUEST_TIMEOUT = 10.0
SNAPSHOT_TIMEOUT = 120.0

# How often to retry connecting while waiting for a freshly spawned
# firecracker to create and bind its API socket.
_READY_POLL = 0.01


class ApiError(RuntimeError):
    """A Firecracker API call that came back as anything but 2xx.

    Firecracker's error bodies are JSON with a ``fault_message`` and they are
    usually the only clue about what was wrong with a config call, so they go
    in the message verbatim.
    """

    def __init__(self, method: str, path: str, status: int, body: bytes) -> None:
        self.method = method
        self.path = path
        self.status = status
        self.body = body
        detail = fault_message(body) or body.decode("utf-8", "replace").strip()
        super().__init__(f"{method} {path} → HTTP {status}{': ' + detail if detail else ''}")


@dataclass(frozen=True)
class Response:
    status: int
    body: bytes

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300


def fault_message(body: bytes) -> str:
    """Pull Firecracker's ``fault_message`` out of an error body.

    Returns "" for anything that isn't the shape we expect — this runs while
    reporting some other failure, so it must never raise one of its own.
    """
    try:
        parsed = json.loads(body)
    except (ValueError, TypeError):
        return ""
    if isinstance(parsed, dict):
        message = parsed.get("fault_message")
        if isinstance(message, str):
            return message
    return ""


def encode_request(method: str, path: str, body: Any = None) -> bytes:
    """Render one HTTP/1.1 request.

    ``Connection: close`` because this opens a fresh socket per call: with
    only a handful of calls per microVM, a connection per call is cheaper to
    get right than a pooled one, and it means a failed call can never leave a
    half-read response poisoning the next.
    """
    payload = b"" if body is None else json.dumps(body).encode()
    lines = [
        f"{method} {path} HTTP/1.1",
        f"Host: {HOST_HEADER}",
        "Accept: application/json",
        "Connection: close",
        f"Content-Length: {len(payload)}",
    ]
    if payload:
        lines.append("Content-Type: application/json")
    return ("\r\n".join(lines) + "\r\n\r\n").encode() + payload


def parse_head(head: bytes) -> tuple[int, dict[str, str]]:
    """Parse a status line plus headers into ``(status, headers)``.

    Header names come back lowercased; *head* is expected to include the
    blank line that ends it, as ``readuntil(b"\\r\\n\\r\\n")`` produces.
    """
    text = head.decode("iso-8859-1")
    lines = text.split("\r\n")
    parts = lines[0].split(" ", 2)
    if len(parts) < 2 or not parts[0].startswith("HTTP/"):
        raise ValueError(f"not an HTTP response: {lines[0]!r}")
    try:
        status = int(parts[1])
    except ValueError as exc:
        raise ValueError(f"bad status line: {lines[0]!r}") from exc

    headers: dict[str, str] = {}
    for line in lines[1:]:
        if not line:
            continue
        name, _, value = line.partition(":")
        headers[name.strip().lower()] = value.strip()
    return status, headers


async def read_response(reader: asyncio.StreamReader) -> Response:
    """Read one response off *reader*.

    Content-Length only. Firecracker never chunks, and quietly treating a
    chunked body as if it were not would corrupt the message of whatever
    error we were in the middle of reporting.
    """
    head = await reader.readuntil(b"\r\n\r\n")
    status, headers = parse_head(head)
    if "transfer-encoding" in headers:
        raise ValueError(f"unsupported transfer-encoding: {headers['transfer-encoding']!r}")
    length = int(headers.get("content-length", "0") or "0")
    body = await reader.readexactly(length) if length else b""
    return Response(status, body)


class FirecrackerApi:
    """A client for one microVM's API socket."""

    def __init__(self, socket_path: Path) -> None:
        self.socket_path = socket_path

    # The `timeout` parameters below are ASYNC109 exceptions on purpose. The
    # rule's advice — let the caller wrap the await in `asyncio.timeout` — is
    # right when a timeout is the caller's policy. Here it is the call's own:
    # a slow snapshot is normal and a slow /machine-config is a hang, and
    # only this module knows which is which. It also lets a timeout surface
    # as an ApiError like every other failure of a call, instead of as a
    # TimeoutError the caller has to special-case.
    async def request(
        self,
        method: str,
        path: str,
        body: Any = None,
        timeout: float = REQUEST_TIMEOUT,  # noqa: ASYNC109
    ) -> Response:
        try:
            async with asyncio.timeout(timeout):
                response = await self._exchange(method, path, body)
        except TimeoutError as exc:
            raise ApiError(method, path, 0, b'{"fault_message":"timed out"}') from exc
        if not response.ok:
            raise ApiError(method, path, response.status, response.body)
        log.debug("firecracker %s %s → %s", method, path, response.status)
        return response

    async def _exchange(self, method: str, path: str, body: Any) -> Response:
        # Resolved at call time, not import time: asyncio only defines this on
        # platforms with AF_UNIX, and this module has to stay importable on a
        # Windows dev box so its pure functions can be tested there.
        reader, writer = await asyncio.open_unix_connection(str(self.socket_path))
        try:
            writer.write(encode_request(method, path, body))
            await writer.drain()
            return await read_response(reader)
        finally:
            writer.close()
            # Firecracker closes its end first (Connection: close), so this
            # is normally already done; swallow whatever a half-dead socket
            # raises rather than masking the real failure above.
            try:
                await writer.wait_closed()
            except OSError:
                log.debug("api socket already closed")

    async def put(
        self,
        path: str,
        body: Any = None,
        timeout: float = REQUEST_TIMEOUT,  # noqa: ASYNC109
    ) -> Response:
        return await self.request("PUT", path, body, timeout)

    async def patch(self, path: str, body: Any = None) -> Response:
        return await self.request("PATCH", path, body)

    async def wait_ready(self, timeout: float) -> None:  # noqa: ASYNC109
        """Block until the API socket accepts a connection.

        A freshly spawned firecracker creates its socket a beat after exec,
        so the first configuration call would otherwise race it. Polling a
        connect is the only honest readiness signal — the socket file
        appearing on disk happens before ``listen()``.
        """
        deadline = time.monotonic() + timeout
        last: OSError | None = None
        while time.monotonic() < deadline:
            try:
                _, writer = await asyncio.open_unix_connection(str(self.socket_path))
            except (OSError, asyncio.CancelledError) as exc:
                if isinstance(exc, asyncio.CancelledError):
                    raise
                last = exc
                await asyncio.sleep(_READY_POLL)
                continue
            writer.close()
            try:
                await writer.wait_closed()
            except OSError:
                pass
            return
        raise TimeoutError(f"firecracker API socket {self.socket_path} not ready: {last}")
