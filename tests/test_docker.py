"""Unit tests for per-room container overrides (no daemon required)."""

from __future__ import annotations

from keycard.backends.docker import ROOM_DEFAULTS, _room_overrides
from keycard.config import RoomConfig


def test_unconfigured_room_only_carries_pids_limit_default() -> None:
    room = RoomConfig(name="ubuntu", image="ubuntu:24.04")
    overrides = _room_overrides(room)
    # No memory/cpus/network configured, so the blanket defaults in
    # ROOM_DEFAULTS should be left alone.
    assert overrides == {"pids_limit": ROOM_DEFAULTS["pids_limit"]}


def test_memory_override() -> None:
    room = RoomConfig(name="python", image="python:3.12-slim", memory="512m")
    overrides = _room_overrides(room)
    assert overrides["mem_limit"] == "512m"


def test_cpus_converted_to_nano_cpus() -> None:
    room = RoomConfig(name="python", image="python:3.12-slim", cpus=2)
    overrides = _room_overrides(room)
    assert overrides["nano_cpus"] == 2_000_000_000


def test_zero_cpus_means_uncapped() -> None:
    room = RoomConfig(name="python", image="python:3.12-slim", cpus=0)
    overrides = _room_overrides(room)
    assert "nano_cpus" not in overrides


def test_pids_limit_is_always_applied() -> None:
    room = RoomConfig(name="node", image="node:22-slim", pids_limit=64)
    overrides = _room_overrides(room)
    assert overrides["pids_limit"] == 64


def test_network_none_isolates_the_room() -> None:
    room = RoomConfig(name="alpine", image="alpine:latest", network="none")
    overrides = _room_overrides(room)
    assert overrides["network_mode"] == "none"


def test_default_network_leaves_bridge_untouched() -> None:
    room = RoomConfig(name="ubuntu", image="ubuntu:24.04")
    overrides = _room_overrides(room)
    assert "network_mode" not in overrides


def test_all_overrides_combine() -> None:
    room = RoomConfig(
        name="alpine",
        image="alpine:latest",
        memory="256m",
        cpus=1,
        pids_limit=32,
        network="none",
    )
    overrides = _room_overrides(room)
    assert overrides == {
        "mem_limit": "256m",
        "nano_cpus": 1_000_000_000,
        "pids_limit": 32,
        "network_mode": "none",
    }
