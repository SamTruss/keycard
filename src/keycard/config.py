"""Configuration loader.

Reads ``keycard.toml`` if it exists, otherwise falls back to built-in
defaults.  The file is optional — keycard must work with zero configuration.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger(__name__)

CONFIG_DIR = Path.home() / ".config" / "keycard"
DEFAULT_CONFIG = CONFIG_DIR / "keycard.toml"

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
    rooms: dict[str, RoomConfig] = field(default_factory=dict)

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
        )
    return rooms


def _builtin_config() -> Config:
    """The zero-config default: three rooms, ubuntu as the fallback."""
    rooms = {
        name: RoomConfig(name=name, image=str(v["image"])) for name, v in BUILTIN_ROOMS.items()
    }
    return Config(rooms=rooms, default_room=DEFAULT_ROOM)


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

    cfg.rooms = _parse_rooms(raw)

    if not cfg.rooms:
        log.info("config has no [rooms.*] — adding built-in defaults")
        cfg = _builtin_config()
        cfg.authorized_keys = Path(
            str(raw.get("authorized_keys", cfg.authorized_keys))
        ).expanduser()
        cfg.host_key = Path(str(raw.get("host_key", cfg.host_key))).expanduser()

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
