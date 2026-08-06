"""Tests for the bits that do not need a container runtime."""

import sys
from pathlib import Path

import asyncssh
import pytest

from keycard.config import Config
from keycard.server import KeycardServer, check_authorized_keys, ensure_host_key


def test_missing_authorized_keys_explains_the_fix(tmp_path: Path) -> None:
    missing = tmp_path / "authorized_keys"
    with pytest.raises(FileNotFoundError) as exc:
        check_authorized_keys(missing)
    # The error should be actionable, not just "file not found".
    assert "cp ~/.ssh/id_ed25519.pub" in str(exc.value)


def test_authorized_keys_present_passes(tmp_path: Path) -> None:
    keys = tmp_path / "authorized_keys"
    keys.write_text("ssh-ed25519 AAAA... test\n")
    check_authorized_keys(keys)


def test_host_key_generated_once_and_locked_down(tmp_path: Path) -> None:
    path = tmp_path / "host_key"
    ensure_host_key(path)

    assert path.exists()
    assert path.with_suffix(".pub").exists()
    if sys.platform != "win32":
        # Windows has no POSIX mode bits; chmod only toggles the read-only
        # flag there. keycard serves from Linux/macOS, so this is the platform
        # that matters — Windows is a dev-only environment.
        assert path.stat().st_mode & 0o777 == 0o600
    # It must load as a real key, not just be a file we made.
    asyncssh.read_private_key(str(path))

    # Regenerating would break every client with a host key mismatch.
    before = path.read_bytes()
    ensure_host_key(path)
    assert path.read_bytes() == before


def test_auth_is_always_required() -> None:
    cfg = Config()
    server = KeycardServer(backend=None, config=cfg)  # type: ignore[arg-type]
    # begin_auth returning False would let anyone in without a key.
    assert server.begin_auth("guest") is True
    assert server.password_auth_supported() is False
    assert server.public_key_auth_supported() is True
