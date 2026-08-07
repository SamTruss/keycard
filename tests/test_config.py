"""Tests for config loading, room resolution, and edge cases."""

from __future__ import annotations

from pathlib import Path

import pytest

from keycard.config import Config, RoomConfig, load, parse_duration


def test_builtin_defaults_when_no_file(tmp_path: Path) -> None:
    cfg = load(tmp_path / "nonexistent.toml")
    assert "ubuntu" in cfg.rooms
    assert "python" in cfg.rooms
    assert "node" in cfg.rooms
    assert cfg.default_room == "ubuntu"


def test_resolve_known_username() -> None:
    cfg = Config(
        rooms={
            "python": RoomConfig(name="python", image="python:3.12-slim"),
            "ubuntu": RoomConfig(name="ubuntu", image="ubuntu:24.04"),
        },
        default_room="ubuntu",
    )
    room = cfg.resolve("python")
    assert room is not None
    assert room.image == "python:3.12-slim"


def test_resolve_unknown_username_falls_back_to_default() -> None:
    cfg = Config(
        rooms={
            "ubuntu": RoomConfig(name="ubuntu", image="ubuntu:24.04"),
        },
        default_room="ubuntu",
    )
    room = cfg.resolve("doesnotexist")
    assert room is not None
    assert room.name == "ubuntu"


def test_resolve_returns_none_when_no_rooms() -> None:
    cfg = Config(rooms={}, default_room="ubuntu")
    assert cfg.resolve("anything") is None


def test_load_custom_config(tmp_path: Path) -> None:
    toml = tmp_path / "keycard.toml"
    toml.write_text(
        """
listen = ":3333"
idle_timeout = "30m"
default_room = "alpine"

[rooms.alpine]
image = "alpine:latest"
memory = "512m"
cpus = 1
network = "none"

[rooms.debian]
image = "debian:bookworm"
""",
        encoding="utf-8",
    )
    cfg = load(toml)
    assert cfg.port == 3333
    assert cfg.idle_timeout == "30m"
    assert cfg.default_room == "alpine"
    assert len(cfg.rooms) == 2

    alpine = cfg.rooms["alpine"]
    assert alpine.image == "alpine:latest"
    assert alpine.memory == "512m"
    assert alpine.cpus == 1
    assert alpine.network == "none"

    debian = cfg.rooms["debian"]
    assert debian.image == "debian:bookworm"
    assert debian.memory == ""


def test_config_with_no_rooms_falls_back_to_builtins(tmp_path: Path) -> None:
    toml = tmp_path / "keycard.toml"
    toml.write_text('listen = ":4444"\n', encoding="utf-8")
    cfg = load(toml)
    assert "ubuntu" in cfg.rooms
    assert "python" in cfg.rooms


def test_host_and_port_parsing() -> None:
    cfg = Config(listen=":2222")
    assert cfg.host == ""
    assert cfg.port == 2222

    cfg = Config(listen="0.0.0.0:3333")
    assert cfg.host == "0.0.0.0"
    assert cfg.port == 3333


@pytest.mark.parametrize(
    ("spec", "seconds"),
    [
        ("15m", 900.0),
        ("30s", 30.0),
        ("1h", 3600.0),
        ("2h", 7200.0),
        ("90", 90.0),
        ("0", 0.0),
        ("", 0.0),
    ],
)
def test_parse_duration(spec: str, seconds: float) -> None:
    assert parse_duration(spec) == seconds


def test_parse_duration_rejects_garbage() -> None:
    with pytest.raises(ValueError):
        parse_duration("soon")


def test_idle_timeout_seconds_property() -> None:
    cfg = Config(idle_timeout="15m")
    assert cfg.idle_timeout_seconds == 900.0

    cfg = Config(idle_timeout="0")
    assert cfg.idle_timeout_seconds == 0.0


def test_shutdown_grace_seconds_property() -> None:
    cfg = Config(shutdown_grace="30s")
    assert cfg.shutdown_grace_seconds == 30.0

    cfg = Config(shutdown_grace="0")
    assert cfg.shutdown_grace_seconds == 0.0


def test_keep_window_seconds_property() -> None:
    cfg = Config(keep_window="10m")
    assert cfg.keep_window_seconds == 600.0

    cfg = Config()
    assert cfg.keep_window_seconds == 0.0  # off by default


def test_keep_window_loaded_from_toml(tmp_path: Path) -> None:
    toml = tmp_path / "keycard.toml"
    toml.write_text(
        'keep_window = "10m"\n\n[rooms.ubuntu]\nimage = "ubuntu:24.04"\n',
        encoding="utf-8",
    )
    cfg = load(toml)
    assert cfg.keep_window == "10m"
    assert cfg.keep_window_seconds == 600.0


def test_shutdown_grace_loaded_from_toml(tmp_path: Path) -> None:
    toml = tmp_path / "keycard.toml"
    toml.write_text(
        'shutdown_grace = "1m"\n\n[rooms.ubuntu]\nimage = "ubuntu:24.04"\n',
        encoding="utf-8",
    )
    cfg = load(toml)
    assert cfg.shutdown_grace == "1m"
    assert cfg.shutdown_grace_seconds == 60.0


def test_room_without_image_is_skipped(tmp_path: Path) -> None:
    toml = tmp_path / "keycard.toml"
    toml.write_text(
        """
[rooms.broken]
memory = "1g"

[rooms.valid]
image = "ubuntu:24.04"
""",
        encoding="utf-8",
    )
    cfg = load(toml)
    assert "broken" not in cfg.rooms
    assert "valid" in cfg.rooms
