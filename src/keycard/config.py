"""Configuration loader.

Reads ``keycard.toml`` if it exists, otherwise falls back to built-in
defaults.  The file is optional — keycard must work with zero configuration.
"""

from __future__ import annotations

import logging
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger(__name__)

CONFIG_DIR = Path.home() / ".config" / "keycard"
DEFAULT_CONFIG = CONFIG_DIR / "keycard.toml"

# Which implementations `backend = "..."` may name, at the top level or on a
# room. Kept here rather than in backends/ because config is loaded long
# before any backend is constructed, and importing a backend to validate a
# string would drag its dependencies in on every `keycard rooms`.
# `tests/test_routing.py` asserts this stays in step with the real registry.
KNOWN_BACKENDS = frozenset({"docker", "firecracker"})
DEFAULT_BACKEND = "docker"

# Ships out of the box so ``ssh ubuntu@host`` works without a config file.
BUILTIN_ROOMS: dict[str, dict[str, object]] = {
    "ubuntu": {"image": "ubuntu:24.04"},
    "python": {"image": "python:3.12-slim"},
    "node": {"image": "node:22-slim"},
}
DEFAULT_ROOM = "ubuntu"

_DURATION_UNITS = {"s": 1.0, "m": 60.0, "h": 3600.0}


def parse_duration(spec: str) -> float:
    """Parse a duration like ``"15m"``, ``"30s"``, ``"1h"``, or a bare number
    of seconds. ``"0"`` (or an empty string) disables whatever it configures.
    """
    spec = spec.strip()
    if not spec or spec == "0":
        return 0.0
    unit = _DURATION_UNITS.get(spec[-1])
    if unit is not None:
        return float(spec[:-1]) * unit
    return float(spec)


@dataclass
class RoomConfig:
    """One entry under ``[rooms.*]``."""

    name: str
    image: str
    memory: str = ""
    cpus: int = 0
    pids_limit: int = 512
    network: str = ""  # "" means default docker bridge; "none" disables
    backend: str = ""  # "" means the server-level default
    rootfs: str = ""  # firecracker only; "" derives it from the room name


# The cmdline `rootfs/build.sh` says its images expect, and the only one that
# matches `rootfs/init.sh` — `init=` has to name keycard-init, and the agent
# writes the guest console to ttyS0. Overridable per deployment, but changing
# it without changing the rootfs is how you get a microVM that boots to
# nothing.
DEFAULT_BOOT_ARGS = "init=/usr/sbin/keycard-init console=ttyS0 reboot=k panic=1 pci=off"


@dataclass
class FirecrackerConfig:
    """The ``[firecracker]`` table.

    Only read when something actually selects the firecracker backend, so a
    Docker-only deployment never has to supply any of it — see
    ``backends/routing.py``, which constructs backends lazily.
    """

    binary: str = "firecracker"
    # No default worth guessing: a guest kernel is a file the operator builds
    # or downloads, and silently booting the wrong one is worse than refusing
    # to start. None means "not configured".
    kernel: Path | None = None
    rootfs_dir: Path = Path("/var/lib/keycard/rootfs")
    # Per-microVM scratch: API socket, vsock socket, the room's rootfs copy,
    # and any snapshot. Deliberately not under rootfs_dir — that one holds
    # the shared built images and is never written to at runtime.
    runtime_dir: Path = Path(tempfile.gettempdir()) / "keycard"
    boot_args: str = DEFAULT_BOOT_ARGS
    boot_timeout: str = "30s"

    @property
    def boot_timeout_seconds(self) -> float:
        """How long to wait for the guest agent to answer after
        InstanceStart. Covers kernel boot plus the rootfs init, so it is
        generous by design — a slow first boot is not a failed one."""
        return parse_duration(self.boot_timeout)


@dataclass
class Config:
    """The full resolved configuration."""

    listen: str = ":2222"
    authorized_keys: Path = CONFIG_DIR / "authorized_keys"
    host_key: Path = CONFIG_DIR / "host_key"
    idle_timeout: str = "15m"
    shutdown_grace: str = "30s"
    keep_window: str = "0"
    default_room: str = DEFAULT_ROOM
    backend: str = DEFAULT_BACKEND
    rooms: dict[str, RoomConfig] = field(default_factory=dict)
    firecracker: FirecrackerConfig = field(default_factory=FirecrackerConfig)

    @property
    def port(self) -> int:
        return int(self.listen.rsplit(":", 1)[-1])

    @property
    def host(self) -> str:
        h = self.listen.rsplit(":", 1)[0]
        return h if h else ""

    @property
    def idle_timeout_seconds(self) -> float:
        """Zero means the idle reaper is disabled."""
        return parse_duration(self.idle_timeout)

    @property
    def shutdown_grace_seconds(self) -> float:
        """How long a graceful shutdown waits for sessions to finish on their
        own before cutting them off. Zero (or unset) means no grace at all —
        the same convention ``docker stop``/``systemctl`` use for a 0 timeout.
        """
        return parse_duration(self.shutdown_grace)

    @property
    def keep_window_seconds(self) -> float:
        """How long a disconnected room stays paused, waiting to be resumed.

        Zero (the default) disables `--keep` entirely: a dropped connection
        is destroyed immediately, same as v1 before this existed.
        """
        return parse_duration(self.keep_window)

    def resolve(self, username: str) -> RoomConfig | None:
        """Map a username to a room. Returns None if unrecognised."""
        return self.rooms.get(username) or self.rooms.get(self.default_room)


def _valid_backend(name: str, where: str) -> str:
    """Reject an unknown ``backend =`` at load time, not on first connection.

    Falls back rather than raising, matching how an unresolvable
    ``default_room`` is handled: a typo in one key shouldn't stop the server
    starting, but it must be loud.
    """
    if not name or name in KNOWN_BACKENDS:
        return name
    log.warning(
        "unknown backend %r in %s (known: %s); using %s",
        name,
        where,
        ", ".join(sorted(KNOWN_BACKENDS)),
        DEFAULT_BACKEND,
    )
    return DEFAULT_BACKEND


def _parse_rooms(raw: dict[str, object]) -> dict[str, RoomConfig]:
    rooms: dict[str, RoomConfig] = {}
    rooms_table = raw.get("rooms", {})
    if not isinstance(rooms_table, dict):
        return rooms
    for name, val in rooms_table.items():
        if not isinstance(val, dict):
            continue
        image = val.get("image")
        if not isinstance(image, str):
            log.warning("room %s has no image; skipping", name)
            continue
        rooms[name] = RoomConfig(
            name=name,
            image=image,
            memory=str(val.get("memory", "")),
            cpus=int(val.get("cpus", 0)),
            pids_limit=int(val.get("pids_limit", 512)),
            network=str(val.get("network", "")),
            backend=_valid_backend(str(val.get("backend", "")), f"room {name}"),
            rootfs=str(val.get("rootfs", "")),
        )
    return rooms


def _parse_firecracker(raw: dict[str, object]) -> FirecrackerConfig:
    fc = FirecrackerConfig()
    table = raw.get("firecracker", {})
    if not isinstance(table, dict):
        return fc

    if "binary" in table:
        fc.binary = str(table["binary"])
    if "kernel" in table:
        fc.kernel = Path(str(table["kernel"])).expanduser()
    if "rootfs_dir" in table:
        fc.rootfs_dir = Path(str(table["rootfs_dir"])).expanduser()
    if "runtime_dir" in table:
        fc.runtime_dir = Path(str(table["runtime_dir"])).expanduser()
    if "boot_args" in table:
        fc.boot_args = str(table["boot_args"])
    if "boot_timeout" in table:
        fc.boot_timeout = str(table["boot_timeout"])
    return fc


def _builtin_rooms() -> dict[str, RoomConfig]:
    return {name: RoomConfig(name=name, image=str(v["image"])) for name, v in BUILTIN_ROOMS.items()}


def _builtin_config() -> Config:
    """The zero-config default: three rooms, ubuntu as the fallback."""
    return Config(rooms=_builtin_rooms(), default_room=DEFAULT_ROOM)


def load(path: Path | None = None) -> Config:
    """Load config from *path*, falling back to built-in defaults.

    If *path* is None, tries ``~/.config/keycard/keycard.toml``.  If that
    doesn't exist either, returns built-in defaults — the "zero config"
    promise.
    """
    if path is None:
        path = DEFAULT_CONFIG

    if not path.exists():
        log.info("no config at %s — using built-in defaults", path)
        return _builtin_config()

    import tomllib

    text = path.read_text(encoding="utf-8")
    raw = tomllib.loads(text)

    cfg = Config()

    if "listen" in raw:
        cfg.listen = str(raw["listen"])
    if "authorized_keys" in raw:
        cfg.authorized_keys = Path(str(raw["authorized_keys"])).expanduser()
    if "host_key" in raw:
        cfg.host_key = Path(str(raw["host_key"])).expanduser()
    if "idle_timeout" in raw:
        cfg.idle_timeout = str(raw["idle_timeout"])
    if "shutdown_grace" in raw:
        cfg.shutdown_grace = str(raw["shutdown_grace"])
    if "keep_window" in raw:
        cfg.keep_window = str(raw["keep_window"])
    if "default_room" in raw:
        cfg.default_room = str(raw["default_room"])
    if "backend" in raw:
        cfg.backend = _valid_backend(str(raw["backend"]), "the top level") or DEFAULT_BACKEND

    cfg.firecracker = _parse_firecracker(raw)
    cfg.rooms = _parse_rooms(raw)

    if not cfg.rooms:
        log.info("config has no [rooms.*] — adding built-in defaults")
        cfg.rooms = _builtin_rooms()

    if cfg.default_room not in cfg.rooms and cfg.rooms:
        cfg.default_room = next(iter(cfg.rooms))
        log.info("default_room not found; falling back to %s", cfg.default_room)

    log.info(
        "loaded %d room(s) from %s — default: %s",
        len(cfg.rooms),
        path,
        cfg.default_room,
    )
    return cfg
