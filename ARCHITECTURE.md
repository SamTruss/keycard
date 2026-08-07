# Architecture

A map of the codebase for anyone about to touch it. Start here before
[CONTRIBUTING.md](CONTRIBUTING.md)'s design principles — this is the *how*,
that's the *why*.

## The mental model

keycard is an SSH server (via [AsyncSSH](https://asyncssh.readthedocs.io/))
that hands out one container per connection. There's no request/response
API to speak of — a connection *is* the transaction. Two objects per
connection do the work:

```
ssh client                          keycard process
    |                                     |
    |--- TCP + SSH handshake ----->  KeycardServer   (one per connection)
    |--- auth, open a session ---->       |
    |                                creates RoomSession (one per channel)
    |--- pty + shell request ----->       |
    |                                RoomSession opens a Room via a Backend
    |                                     |  (a Docker container, today)
    |<==== raw pty bytes, both ways ======|
    |                                     |
    |--- disconnect / exit -------->  RoomSession tears the Room down
```

`KeycardServer` (`server.py`) is the `asyncssh.SSHServer` — auth posture,
username-to-room resolution, one instance per TCP connection.
`RoomSession` (`session.py`) is the `asyncssh.SSHServerSession` — the actual
pty bridge, one instance per SSH channel. In practice that's a 1:1 pairing,
since v1 only ever opens one channel per connection (`exec` is refused;
interactive only — see `RoomSession.exec_requested`).

## Module map

| File | Responsibility |
|---|---|
| `cli.py` | Click entry point: `up`, `rooms`. Thin — parses flags, loads config, calls into `server.py`. |
| `server.py` | `KeycardServer` (auth, room resolution), `create_server`/`serve` (listen, signal handling, shutdown drain). |
| `session.py` | `RoomSession` (the pty bridge and every teardown path), `ActiveSessions` and `KeptRooms` (the two session registries). |
| `config.py` | `Config`/`RoomConfig` dataclasses, `keycard.toml` parsing, duration parsing (`"15m"` → seconds). |
| `banner.py` | The ASCII banners written to the operator console and the SSH session. Pure string formatting. |
| `backends/base.py` | The `Backend`/`Room`/`Kept` abstract interface — see below. |
| `backends/docker.py` | The only implementation today: `DockerBackend`/`DockerRoom`/`DockerKept`. |

## The backend seam

Nothing above `backends/` knows a container is a container. `Backend`
builds and resumes rooms; `Room` is a single live sandbox with an attached
pty; `Kept` is an opaque handle to a paused-but-not-destroyed room. Three
methods each:

```python
class Backend(abc.ABC):
    async def open(self, room: RoomConfig, width: int, height: int) -> Room: ...
    async def resume(self, kept: Kept, width: int, height: int) -> Room: ...
    async def destroy_kept(self, kept: Kept) -> None: ...
    async def close(self) -> None: ...

class Room(abc.ABC):
    async def read(self) -> bytes: ...
    async def write(self, data: bytes) -> None: ...
    async def resize(self, width: int, height: int) -> None: ...
    async def destroy(self) -> int: ...
    async def pause(self) -> Kept: ...
```

This exists for v2: a `FirecrackerBackend` implementing the same interface
should be a drop-in for `DockerBackend`, with zero changes to `server.py`
or `session.py`. It's also why `RoomSession` never imports anything from
`backends.docker` — if you find yourself reaching for a Docker-specific
type or exception outside `backends/docker.py`, that's the seam leaking.

`Room.destroy()` must be safe to call more than once — see "the many ways
a room ends" below for why that's load-bearing, not defensive paranoia.

## Concurrency model

Everything runs on a single asyncio event loop. The Docker SDK
(`docker-py`) is synchronous, so every call into it — `containers.run()`,
`.pause()`, `.remove()` — is pushed onto the default executor via
`loop.run_in_executor(None, ...)`. That's most of what `DockerRoom`'s
methods look like: a small sync closure, executed off-thread.

The one exception is the actual pty stream. `DockerRoom.read`/`write`
unwrap docker-py's attach-socket wrapper down to the bare OS socket and
drive it directly with `loop.sock_recv`/`sock_sendall` — no reader thread,
no polling. This only works cleanly because every room is created with
`tty=True`, which makes the attach stream raw (no 8-byte multiplex header
to strip); it's the single biggest simplification available in
`backends/docker.py`, and worth preserving if you ever touch it.

## The many ways a room ends

This is the part most worth understanding before changing `session.py`. A
room can be torn down from five different triggers, and they can race each
other — `RoomSession` guards against double-teardown by nulling
`self._room` in the *same synchronous step* that decides to act on it,
before any `await`:

| Trigger | Where | Result |
|---|---|---|
| Shell exits on its own (client typed `exit`) | `RoomSession._run()`'s read loop breaks; its `finally` checks out | Room destroyed, real exit status forwarded |
| Connection drops while the shell is still running | `RoomSession.connection_lost()` | Room destroyed — or, with `--keep` on, paused via `KeptRooms.keep()` instead |
| No traffic for `idle_timeout` | `RoomSession._idle_watchdog()` | Room destroyed, client sees exit `124` |
| `shutdown_grace` expires during server shutdown | `RoomSession.force_close()`, called from `server._drain()` | Room destroyed, client sees exit `143` |
| `--keep` window expires unclaimed | `KeptRooms._expire()` | Paused room destroyed, no client attached |
| Server shuts down with rooms still paused | `KeptRooms.destroy_all()`, called from `serve()` | Every kept room swept, so none outlive the process |

Two things make this safe rather than fragile:

1. **`self._room = None` happens before the teardown call, not after.** Every
   path does `room, self._room = self._room, None` and only then calls
   `_checkout`/`pause`/etc. A second trigger racing in behind it sees
   `self._room is None` and backs off instead of double-tearing-down.
2. **`Room.destroy()` is independently idempotent** (`DockerRoom` guards it
   with a lock and a `_destroyed` flag), as a second line of defence for
   whatever the first guarantee doesn't catch.

A clean shell exit and a dropped connection are deliberately *not* the same
code path: `--keep` only pauses on the latter. Typing `exit` is a
deliberate goodbye and always destroys, regardless of `--keep`.

Graceful shutdown (`server._drain`) and `--keep` interact with the same
registries but don't know about each other: `_drain` only touches
`ActiveSessions` (live connections), then `serve()` separately sweeps
`KeptRooms` afterward. A session being force-closed during shutdown is
never paused — that would mean a room outliving the process with nothing
left to reap it if the server never comes back up.

## Configuration

`config.py` has one entry point, `load()`: try the given path, or
`~/.config/keycard/keycard.toml`, or fall back to `_builtin_config()`
(three built-in rooms, zero-config). Durations (`idle_timeout`,
`shutdown_grace`, `keep_window`) are plain strings in the dataclass,
parsed to seconds on demand via `parse_duration()` — kept as strings so
the *_seconds properties can be accessed lazily and a malformed value
fails at server startup (`create_server` touches all three up front)
rather than silently at whatever moment the reaper eventually fires.

## Testing strategy

Two tiers, split by whether they need a real container runtime:

- **Unit tests** (`test_session.py`, `test_config.py`, `test_server.py`,
  `test_banner.py`, `test_docker.py`) run against fakes — `FakeRoom`,
  `FakeBackend`, `FakeChannel` in `test_session.py` implement the same
  `Room`/`Backend` ABCs the real Docker backend does, so `RoomSession`
  itself is exercised without Docker. Fast, deterministic, no daemon
  required.
- **Integration tests** (`test_integration.py`) drive a real server on an
  ephemeral port against a real Docker daemon, then inspect what actually
  happened to the container. Auto-skip (`_docker_available()`) when no
  daemon is reachable, so `pytest` stays green on a machine without one.

CI mirrors that split: the `test` job (matrix across 3.11–3.13) runs
everything except `test_integration.py`; a separate `integration` job
(single Python version — this isn't testing Python-version-specific
behaviour) runs only that file, with a session-scoped fixture that
pre-pulls the test image so pull latency never eats into a test's own
timing budget.

## Extending keycard

**A new config option:** add the field to `Config`/`RoomConfig` in
`config.py`, parse it in `load()`, document it in
`keycard.example.toml`. If it's a duration, follow `idle_timeout`'s
pattern (string field + a `_seconds` property via `parse_duration`).

**A new CLI flag:** add it to the relevant `@click.option` in `cli.py`,
have it override the loaded `Config` before `serve()` is called (see how
`--host`/`--port`/`--keep` do it), and update `man/keycard.1` — nothing
regenerates that file, it's hand-maintained.

**A new backend (v2):** implement `Backend`/`Room`/`Kept` from
`backends/base.py`. Nothing in `server.py` or `session.py` should need to
change; if it does, that's a sign the abstraction has a gap worth fixing
first.
