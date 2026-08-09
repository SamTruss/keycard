"""Every place the version is written down has to agree with the package.

`release.yml` already refuses to build if the tag and `pyproject.toml`
disagree, which is why those two have never drifted. Nothing watched the
other five, and they drifted exactly as you would expect: the README, both
published pages and the man page sat at 0.2.1 while 0.2.2 shipped, because
the release checklist named only the two files the workflow checks.

These are the user-facing ones — the footer of the page someone lands on,
and what `man keycard` prints. Getting them wrong tells a reader they are
looking at documentation for a release that is not the one they installed.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

import pytest

from keycard import __version__

ROOT = Path(__file__).resolve().parent.parent


def test_pyproject_agrees_with_the_package() -> None:
    """The one pair `release.yml` already enforces — asserted here too so a
    mismatch fails in the suite rather than only at tag time, when the fix
    means retagging."""
    raw = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert raw["project"]["version"] == __version__


@pytest.mark.parametrize(
    ("relative", "pattern"),
    [
        ("README.md", r"v(\d+\.\d+\.\d+)\s+&nbsp;·&nbsp;\s+built in the terminal"),
        ("man/keycard.1", r'\.TH KEYCARD 1 "[\d-]+" "keycard (\d+\.\d+\.\d+)"'),
        ("docs/index.html", r"<dt>v(\d+\.\d+\.\d+)</dt>"),
        ("docs/index.html", r"<div>v(\d+\.\d+\.\d+) · MIT"),
        ("docs/404.html", r"<div>v(\d+\.\d+\.\d+) &middot; MIT"),
    ],
)
def test_documented_version_matches_the_package(relative: str, pattern: str) -> None:
    text = (ROOT / relative).read_text(encoding="utf-8")
    found = re.search(pattern, text)
    # A pattern that stops matching is itself the failure: it means the line
    # was reworded, and a silently-skipped check is how this drifted before.
    assert found is not None, f"{relative}: no version string matched {pattern!r}"
    assert found.group(1) == __version__, (
        f"{relative} says {found.group(1)}, package is {__version__}"
    )
