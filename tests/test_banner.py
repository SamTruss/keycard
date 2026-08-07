"""Tests for the ASCII banners (pure string formatting, no daemon needed)."""

from __future__ import annotations

from keycard import banner


def test_logo_is_a_rectangle() -> None:
    lines = banner.LOGO.splitlines()
    widths = {len(line) for line in lines}
    assert len(widths) == 1, "every line of the card should be the same width"


def test_logo_is_plain_ascii() -> None:
    banner.LOGO.encode("ascii")  # raises if anything outside ASCII sneaks in


def test_logo_names_the_project() -> None:
    # Letter-spaced ("k e y c a r d") for style; strip the spacing to check.
    assert "keycard" in banner.LOGO.replace(" ", "")


def test_accepted_names_the_room() -> None:
    out = banner.accepted("python", "python:3.12-slim").decode()
    assert "KEYCARD ACCEPTED" in out
    assert "python" in out
    assert "python:3.12-slim" in out
    assert out.endswith("\r\n")


def test_destroyed_carries_the_reason() -> None:
    out = banner.destroyed("idle timeout").decode()
    assert "ROOM DESTROYED" in out
    assert "idle timeout" in out
    assert out.endswith("\r\n")
