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
    |                                     |  (a Docker container, or a microVM)
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
| `backends/docker.py` | `DockerBackend`/`DockerRoom`/`DockerKept`. The default, and the only one that has shipped. |
| `backends/firecracker.py` | `FirecrackerBackend`/`FirecrackerRoom`/`FirecrackerKept`: rooms as microVMs. Opt-in, still incomplete, and `--keep` is refused here (`can_pause = False`) — see [FIRECRACKER.md](FIRECRACKER.md). |
| `backends/vsock.py` | The vsock plumbing under that backend: the Firecracker `CONNECT`/`OK` handshake, and the two channels (pty data, control) the guest agent speaks over. |
| `backends/fcapi.py` | Firecracker's REST-over-a-UNIX-socket control API, which is how a VM gets configured, booted, snapshotted and restored. |
| `backends/routing.py` | `RoutingBackend`: itself a `Backend`, so that per-room backend selection stays invisible above the seam. |
| `backends/confine.py` | Landlock and rlimits for the VMM process — what the Firecracker jailer would have done, minus the root it would have needed. |

Two trees outside `src/` belong to the microVM backend: `guest-agent/` (the
Rust pty agent that runs as PID 1 inside a room) and `rootfs/` (the script
that turns a room's Docker image into a bootable ext4 filesystem with that
agent installed).

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

This seam existed before there was a second backend to justify it, and it
held: `FirecrackerBackend` implements the same five-and-five methods and
`server.py` never learned it exists. It's also why `RoomSession` never
imports anything from `backends.docker` — if you find yourself reaching for
a Docker-specific type or exception outside `backends/docker.py`, that's
the seam leaking.

`session.py` did change when the second backend arrived, but not to
accommodate it: a microVM takes seconds to boot where a container takes
about one, and that longer `open()` exposed a latent bug where input typed
before the room existed was dropped on the floor. The fix (queue it, drain
it once there's a pty) is backend-agnostic and was always the correct
behaviour. A slow backend as a bug-finder is worth expecting again.

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
room can be torn down from six different triggers, and they can race each
other:

| Trigger | Where | Result |
|---|---|---|
| Shell exits on its own (client typed `exit`) | `RoomSession._run()`'s read loop breaks; its `finally` checks out | Room destroyed, real exit status forwarded |
| Connection drops while the shell is still running | `RoomSession.connection_lost()` | Room destroyed — or, with `--keep` on *and* the room's backend willing to pause, held via `KeptRooms.keep()` instead |
| No traffic for `idle_timeout` | `RoomSession._idle_watchdog()` | Room destroyed, client sees exit `124` |
| `shutdown_grace` expires during server shutdown | `RoomSession.force_close()`, called from `server._drain()` | Room destroyed, client sees exit `143` |
| `--keep` window expires unclaimed | `KeptRooms._expire()` | Paused room destroyed, no client attached |
| Server shuts down with rooms still paused | `KeptRooms.destroy_all()`, called from `serve()` | Every kept room swept, so none outlive the process |

The first four all funnel into **one teardown task per session**:
`_begin_teardown()` creates it if it does not exist and otherwise hands back
the one already running, so whichever trigger arrives first owns the
teardown and the rest await that same task.

Two things make this safe rather than fragile:

1. **Racing triggers wait, rather than backing off.** It is not enough to
   stop a second trigger from tearing the room down twice — it must also not
   walk away believing teardown is *finished* when the first one is still
   inside `destroy()`. `RoomSession._release()` is what tells a shutdown
   drain the session is done, so a trigger that returns early there lets
   `serve()` reach `backend.close()` and process exit with a destroy still in
   flight, which `asyncio.run` then cancels — leaving a container running or
   a microVM's RAM written out to disk with nothing owning it. Every caller
   getting the same awaitable task back is what closes that window, and
   `_release()` fires in that task's `finally`, once.
2. **`Room.destroy()` is independently idempotent** (`DockerRoom` and
   `FirecrackerRoom` each guard it with a lock and a `_destroyed` flag), as a
   second line of defence for whatever the first guarantee doesn't catch.

Holding teardown as a *task* rather than running it inline matters for the
same reason: `connection_lost` cancels `_pump`, and a destroy running inline
inside that task's `finally` would be cancelled along with it.

A clean shell exit and a dropped connection are deliberately *not* the same
code path: `--keep` only pauses on the latter. Typing `exit` is a
deliberate goodbye and always destroys, regardless of `--keep`. That is what
`allow_keep` on `_begin_teardown` selects, and only `connection_lost` passes
it — so whichever trigger wins the race also decides whether the room is
eligible to be paused.

Eligibility has a second half: `Room.can_pause`. A backend sets it False when
freezing a room would cost more than the reconnect window is worth, and
firecracker does — a snapshot writes the whole of guest RAM to disk in the
clear. Those rooms are destroyed on disconnect however `keep_window` is set.
It lives on the room rather than the config because the room is the only
thing that still knows which backend actually built it: `backend` can be set
per room or inherited from the top level, and by teardown the session has
neither. `Backend.can_pause` mirrors it so `serve()` can warn at boot, before
any room exists, and a test pins the two together.

Graceful shutdown (`server._drain`) and `--keep` interact with the same
registries but don't know about each other: `_drain` only touches
`ActiveSessions` (live connections), then `serve()` separately sweeps
`KeptRooms` afterward. A session force-closed during shutdown is never
paused — that would mean a room outliving the process with nothing left to
reap it if the server never comes back up. A client that hung up moments
*before* the drain reached it is the one exception: its teardown was already
started with `allow_keep=True`, so it pauses, and `KeptRooms.destroy_all()`
sweeps it a moment later. The ordering in `serve()` is what makes that safe —
the drain waits for the pause to land, so the sweep that follows can see it.

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

Three tiers, split by what each one needs from the host:

- **Unit tests** — everything except the two `*_integration.py` files. Most
  run against fakes: `FakeRoom`, `FakeBackend` and `FakeChannel` in
  `test_session.py` implement the same `Room`/`Backend` ABCs the real
  backends do, so `RoomSession` is exercised without Docker. The Firecracker
  side is split so that its decisions (`test_firecracker.py`,
  `test_fcapi.py`, `test_vsock.py`, `test_routing.py`) can be held to
  account on a machine with no `/dev/kvm`. Fast, deterministic, no daemon.
- **Docker integration** (`test_integration.py`) drives a real server on an
  ephemeral port against a real Docker daemon, then inspects what actually
  happened to the container. Auto-skips (`_docker_available()`) when no
  daemon is reachable.
- **Firecracker integration** (`test_firecracker_integration.py`) boots
  actual microVMs, and skips unless `/dev/kvm`, a `firecracker` binary and a
  built rootfs are all present. It names the first missing prerequisite
  rather than skipping silently, because a backend that quietly stops being
  tested is worse than one that visibly can't be.

CI mirrors that split: the `test` job (matrix across 3.11–3.13) runs
everything except `test_integration.py`; a separate `integration` job
(single Python version — this isn't testing Python-version-specific
behaviour) runs only that file, with a session-scoped fixture that pre-pulls
the test image so pull latency never eats into a test's own timing budget.

GitHub's runners have no `/dev/kvm`, so **CI never boots a VM**. Two things
partly cover that gap. The guest agent (`guest-agent/`, Rust) is tested
against a real shell with TCP standing in for vsock — a substitution that is
load-bearing and has already been wrong once: TCP propagates a half-close
and Firecracker's vsock does not, so a bug that hung every room passed CI
green. Treat a passing guest-agent suite as evidence about the agent's
logic, not about the transport. The confinement (`test_confine.py`) is the
happier case: Landlock needs no virtualization, so those tests really do run
on the runners, and they test by denial rather than by success.

## Extending keycard

**A new config option:** add the field to `Config`/`RoomConfig` in
`config.py`, parse it in `load()`, document it in
`keycard.example.toml`. If it's a duration, follow `idle_timeout`'s
pattern (string field + a `_seconds` property via `parse_duration`).

**A new CLI flag:** add it to the relevant `@click.option` in `cli.py`,
have it override the loaded `Config` before `serve()` is called (see how
`--host`/`--port`/`--keep` do it), and update `man/keycard.1` — nothing
regenerates that file, it's hand-maintained.

**A new backend:** implement `Backend`/`Room`/`Kept` from
`backends/base.py`, add a factory to `FACTORIES` in `backends/routing.py`
(deferring the import inside it, so naming a backend is what pays for
importing its stack), and add the name to `KNOWN_BACKENDS` in `config.py`.
Nothing in `server.py` or `session.py` should need to change; if it does,
that's a sign the abstraction has a gap worth fixing first.
`backends/firecracker.py` is the worked example of doing this against
something that isn't a container at all.
