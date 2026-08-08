"""Confinement of the firecracker process.

The interesting tests here are the ones that prove the ruleset *denies*
things, because a confinement that silently allows everything looks
identical to a working one from the outside — the VM boots either way. Each
of those forks, applies the real `child_setup`, and reports back what it
could reach.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

from keycard.backends import confine

landlock = pytest.mark.skipif(
    confine.abi_version() == 0,
    reason="needs a kernel with Landlock (Linux 5.13+)",
)


def probe(ruleset_fd: int | None, paths: dict[str, Path]) -> dict[str, str]:
    """Apply the confinement in a child and report what it could reach.

    A fork rather than a subprocess because `landlock_restrict_self` is
    irreversible: whatever applies it can never take it off again, and that
    must not be the process running the test suite.
    """
    read_fd, write_fd = os.pipe()
    pid = os.fork()
    if pid == 0:  # pragma: no cover - the child never reports coverage
        os.close(read_fd)
        try:
            confine.child_setup(ruleset_fd, fsize=64 * 1024 * 1024)()
            result = {}
            for label, path in paths.items():
                try:
                    with open(path, "rb") as handle:
                        handle.read(1)
                    result[label] = "read"
                except OSError as exc:
                    result[label] = f"denied:{exc.errno}"
            os.write(write_fd, json.dumps(result).encode())
        except BaseException as exc:  # noqa: BLE001 - reported, not raised, across a fork
            os.write(write_fd, json.dumps({"error": repr(exc)}).encode())
        finally:
            os._exit(0)

    os.close(write_fd)
    with os.fdopen(read_fd, "rb") as handle:
        payload = handle.read()
    os.waitpid(pid, 0)
    return dict(json.loads(payload))


@pytest.fixture
def instance(tmp_path: Path) -> Path:
    room = tmp_path / "instance"
    room.mkdir()
    (room / "rootfs.ext4").write_bytes(b"rootfs")
    return room


def test_the_abi_version_is_zero_or_a_version(monkeypatch: pytest.MonkeyPatch) -> None:
    assert confine.abi_version() >= 0
    # A host with no libc has to look exactly like a host with no Landlock,
    # since every caller handles only the second.
    monkeypatch.setattr(confine, "_libc", None)
    assert confine.abi_version() == 0


def test_a_plan_makes_only_the_instance_directory_writable(instance: Path) -> None:
    plan = confine.plan_for(
        binary=Path("/bin/true"), kernel=Path("/etc/hostname"), instance_dir=instance
    )
    assert plan.writable == (instance, confine.KVM)
    assert instance.parent not in plan.writable
    assert plan.readable == (Path("/etc/hostname"),)


def test_extra_readable_paths_are_readable_not_writable(instance: Path) -> None:
    plan = confine.plan_for(
        binary=Path("/bin/true"),
        kernel=Path("/etc/hostname"),
        instance_dir=instance,
        extra_readable=[Path("/sys")],
    )
    assert Path("/sys") in plan.readable
    assert Path("/sys") not in plan.writable


def test_fsize_clears_the_largest_thing_written() -> None:
    # Both floors matter: a rootfs bigger than RAM, and RAM bigger than the
    # rootfs, must each end up under the limit.
    assert confine.fsize_limit(900, 100) > 900
    assert confine.fsize_limit(100, 900) > 900
    assert confine.fsize_limit() == confine.FSIZE_HEADROOM


def test_no_landlock_means_no_ruleset(monkeypatch: pytest.MonkeyPatch, instance: Path) -> None:
    monkeypatch.setattr(confine, "abi_version", lambda: 0)
    plan = confine.plan_for(
        binary=Path("/bin/true"), kernel=Path("/etc/hostname"), instance_dir=instance
    )
    assert confine.build_ruleset(plan) is None


def test_a_path_that_does_not_exist_is_an_error_not_a_silent_pass(instance: Path) -> None:
    """A missing path must not degrade into running unconfined."""
    plan = confine.Plan(readable=(instance / "nope",))
    if confine.abi_version() == 0:
        pytest.skip("needs a kernel with Landlock")
    with pytest.raises(confine.ConfinementError):
        confine.build_ruleset(plan)


@landlock
def test_the_confinement_hides_everything_outside_the_instance(
    tmp_path: Path, instance: Path
) -> None:
    secret = tmp_path / "host_key"
    secret.write_text("a private key")
    sibling = tmp_path / "another-room"
    sibling.mkdir()
    (sibling / "rootfs.ext4").write_text("someone else's room")

    plan = confine.plan_for(
        binary=Path(sys.executable), kernel=Path("/etc/hostname"), instance_dir=instance
    )
    fd = confine.build_ruleset(plan)
    assert fd is not None
    try:
        reached = probe(
            fd,
            {
                "own rootfs": instance / "rootfs.ext4",
                "kernel": Path("/etc/hostname"),
                "host key": secret,
                "another room": sibling / "rootfs.ext4",
            },
        )
    finally:
        os.close(fd)

    assert "error" not in reached, reached
    assert reached["own rootfs"] == "read"
    assert reached["kernel"] == "read"
    # The two that matter. Without confinement both of these are readable,
    # because the VMM runs as the same user keycard does.
    assert reached["host key"].startswith("denied"), reached
    assert reached["another room"].startswith("denied"), reached


@landlock
def test_without_a_ruleset_the_same_paths_are_reachable(tmp_path: Path, instance: Path) -> None:
    """The control for the test above — proof it is the ruleset doing it."""
    secret = tmp_path / "host_key"
    secret.write_text("a private key")

    reached = probe(None, {"host key": secret, "own rootfs": instance / "rootfs.ext4"})

    assert reached == {"host key": "read", "own rootfs": "read"}
