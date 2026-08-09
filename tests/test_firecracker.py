"""The Firecracker backend's decisions, minus the microVM.

Everything here is either a pure function or something that only touches a
directory, which is deliberate: it is the share of Phase 2 that can be held
to account on a machine with no /dev/kvm. Booting one is
`tests/test_firecracker_integration.py`.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from keycard.backends import firecracker as fc
from keycard.config import DEFAULT_BOOT_ARGS, FirecrackerConfig, RoomConfig

# -- memory -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("spec", "expected"),
    [
        ("512m", 512),
        ("1g", 1024),
        ("2G", 2048),
        ("1048576k", 1024),
        ("536870912", 512),  # bare numbers are bytes, as Docker reads them
        ("1.5g", 1536),
    ],
)
def test_memory_parses_the_same_spellings_docker_takes(spec: str, expected: int) -> None:
    assert fc.parse_memory_mib(spec) == expected


def test_unset_memory_falls_back_to_the_docker_backend_default() -> None:
    assert fc.parse_memory_mib("") == fc.DEFAULT_MEM_MIB


def test_unparseable_memory_falls_back_rather_than_failing_to_open() -> None:
    # The Docker backend ignores a memory string it doesn't like; a room
    # shouldn't open on one backend and refuse on the other over the string.
    assert fc.parse_memory_mib("lots") == fc.DEFAULT_MEM_MIB


def test_memory_is_never_below_what_firecracker_accepts() -> None:
    assert fc.parse_memory_mib("1") == fc.MIN_MEM_MIB


# -- machine config ---------------------------------------------------------


def test_machine_config_supplies_both_required_keys_for_a_bare_room() -> None:
    body = fc.machine_config(RoomConfig(name="ubuntu", image="ubuntu:24.04"))
    assert body == {"vcpu_count": fc.DEFAULT_VCPUS, "mem_size_mib": fc.DEFAULT_MEM_MIB}


def test_machine_config_uses_the_rooms_caps() -> None:
    room = RoomConfig(name="python", image="x", memory="2g", cpus=4)
    assert fc.machine_config(room) == {"vcpu_count": 4, "mem_size_mib": 2048}


def test_vcpus_are_clamped_to_what_the_api_allows() -> None:
    room = RoomConfig(name="big", image="x", cpus=999)
    assert fc.machine_config(room)["vcpu_count"] == fc.MAX_VCPUS


# -- control protocol -------------------------------------------------------


def test_exit_status_is_read_off_the_control_channel() -> None:
    assert fc.parse_exit(b"exit 7") == 7


def test_exit_status_zero_is_a_status_not_an_absence() -> None:
    assert fc.parse_exit(b"exit 0") == 0


@pytest.mark.parametrize("line", [b"", b"resize 80 24", b"exit", b"exit x", b"exit 1 2"])
def test_other_control_lines_are_not_mistaken_for_a_status(line: bytes) -> None:
    assert fc.parse_exit(line) is None


# -- process argv -----------------------------------------------------------


def test_argv_points_firecracker_at_the_instances_api_socket() -> None:
    argv = fc.firecracker_argv("/usr/bin/firecracker", Path("/run/keycard/x/api.sock"))
    assert argv == ["/usr/bin/firecracker", "--api-sock", "/run/keycard/x/api.sock"]


# -- instance layout --------------------------------------------------------


def test_instance_puts_everything_a_room_owns_in_one_directory(tmp_path: Path) -> None:
    # This is what makes destroy_kept a single rmtree, which is the answer to
    # FIRECRACKER.md's snapshot-lifecycle question.
    instance = fc.Instance.create(tmp_path, "ubuntu")
    paths = [
        instance.api_sock,
        instance.vsock_sock,
        instance.rootfs,
        instance.snapshot,
        instance.mem_file,
        instance.console_log,
    ]
    assert all(path.parent == instance.dir for path in paths)
    assert instance.dir.is_dir()


def test_an_instance_directory_is_not_readable_by_other_users(tmp_path: Path) -> None:
    """What lands in here is the room's disk image and, once `pause` has
    run, a plaintext dump of its RAM. The default runtime_dir is under /tmp,
    so the usual 0755 would hand both to every local user on the host."""
    instance = fc.Instance.create(tmp_path, "ubuntu")
    assert instance.dir.stat().st_mode & 0o077 == 0


def test_a_pre_existing_runtime_dir_is_tightened_not_trusted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """It lives under /tmp by default, so it may already exist — created
    0755 by an older release, or by whoever got there first."""
    runtime = tmp_path / "run"
    runtime.mkdir(mode=0o755)
    kernel = tmp_path / "vmlinux"
    kernel.write_bytes(b"x")
    monkeypatch.setattr(fc.shutil, "which", lambda _: "/usr/bin/firecracker")
    monkeypatch.setattr(fc, "kvm_present", lambda: True)
    fc.FirecrackerBackend(
        FirecrackerConfig(kernel=kernel, rootfs_dir=tmp_path, runtime_dir=runtime)
    )
    assert runtime.stat().st_mode & 0o077 == 0


def test_instance_ids_are_unique_and_name_their_room(tmp_path: Path) -> None:
    first = fc.Instance.create(tmp_path, "python")
    second = fc.Instance.create(tmp_path, "python")
    assert first.id != second.id
    assert first.id.startswith("python-")


# -- rootfs resolution ------------------------------------------------------


@pytest.fixture
def backend(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> fc.FirecrackerBackend:
    """A backend whose preflight is satisfied by files in tmp_path.

    Preflight is the one part that genuinely needs the host to be capable, so
    the /dev/kvm and PATH checks are stubbed; everything the tests below
    touch is real.
    """
    kernel = tmp_path / "vmlinux"
    kernel.write_bytes(b"not really a kernel")
    (tmp_path / "images").mkdir()
    monkeypatch.setattr(fc.shutil, "which", lambda _: "/usr/bin/firecracker")
    monkeypatch.setattr(fc, "kvm_present", lambda: True)
    config = FirecrackerConfig(
        kernel=kernel,
        rootfs_dir=tmp_path / "images",
        runtime_dir=tmp_path / "run",
    )
    return fc.FirecrackerBackend(config)


def test_rootfs_defaults_to_what_build_sh_writes(
    backend: fc.FirecrackerBackend, tmp_path: Path
) -> None:
    room = RoomConfig(name="python", image="python:3.12-slim")
    assert backend.rootfs_for(room) == tmp_path / "images" / "python.ext4"


def test_a_room_can_name_its_own_image(backend: fc.FirecrackerBackend) -> None:
    room = RoomConfig(name="python", image="x", rootfs="/srv/custom.ext4")
    assert backend.rootfs_for(room) == Path("/srv/custom.ext4")


def test_runtime_dir_is_created_at_startup(backend: fc.FirecrackerBackend, tmp_path: Path) -> None:
    assert (tmp_path / "run").is_dir()


async def test_opening_a_room_with_no_built_rootfs_says_how_to_build_one(
    backend: fc.FirecrackerBackend,
) -> None:
    room = RoomConfig(name="node", image="node:22-slim")
    with pytest.raises(fc.FirecrackerError, match="rootfs/build.sh --room node"):
        await backend.open(room, 80, 24)


# -- preflight --------------------------------------------------------------


def test_missing_kernel_is_a_startup_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(fc.shutil, "which", lambda _: "/usr/bin/firecracker")
    monkeypatch.setattr(fc, "kvm_present", lambda: True)
    with pytest.raises(fc.FirecrackerError, match="needs a guest kernel"):
        fc.FirecrackerBackend(FirecrackerConfig(rootfs_dir=tmp_path))


def test_missing_binary_is_a_startup_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(fc.shutil, "which", lambda _: None)
    with pytest.raises(fc.FirecrackerError, match="not found on PATH"):
        fc.FirecrackerBackend(FirecrackerConfig(rootfs_dir=tmp_path))


def test_absent_kvm_is_refused_rather_than_discovered_on_first_boot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    kernel = tmp_path / "vmlinux"
    kernel.write_bytes(b"x")
    monkeypatch.setattr(fc.shutil, "which", lambda _: "/usr/bin/firecracker")
    monkeypatch.setattr(fc, "kvm_present", lambda: False)
    config = FirecrackerConfig(kernel=kernel, rootfs_dir=tmp_path)
    with pytest.raises(fc.FirecrackerError, match="/dev/kvm"):
        fc.FirecrackerBackend(config)


# -- confinement ------------------------------------------------------------


def _bootable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, **kwargs: object
) -> FirecrackerConfig:
    """A config whose only remaining question is the one under test."""
    kernel = tmp_path / "vmlinux"
    kernel.write_bytes(b"x")
    monkeypatch.setattr(fc.shutil, "which", lambda _: "/usr/bin/firecracker")
    monkeypatch.setattr(fc, "kvm_present", lambda: True)
    return FirecrackerConfig(kernel=kernel, rootfs_dir=tmp_path, **kwargs)  # type: ignore[arg-type]


def test_required_confinement_refuses_a_kernel_without_landlock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The whole point of `required`: an operator who asked for confinement
    gets a server that will not run without it, rather than one that quietly
    runs the VMM with keycard's own filesystem access."""
    monkeypatch.setattr(fc.confine, "abi_version", lambda: 0)
    config = _bootable(tmp_path, monkeypatch, runtime_dir=tmp_path / "run", confinement="required")
    with pytest.raises(fc.FirecrackerError, match="no Landlock"):
        fc.FirecrackerBackend(config)


def test_best_effort_still_starts_without_landlock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(fc.confine, "abi_version", lambda: 0)
    config = _bootable(
        tmp_path, monkeypatch, runtime_dir=tmp_path / "run", confinement="best-effort"
    )
    fc.FirecrackerBackend(config)
    assert "unconfined" in caplog.text


def test_confinement_off_builds_no_ruleset(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = _bootable(tmp_path, monkeypatch, runtime_dir=tmp_path / "run", confinement="off")
    backend = fc.FirecrackerBackend(config)
    instance = fc.Instance.create(config.runtime_dir, "ubuntu")
    assert backend._ruleset_for(instance) is None  # noqa: SLF001 - the decision under test


# -- boot args --------------------------------------------------------------


def test_default_boot_args_name_the_init_the_rootfs_actually_ships() -> None:
    # rootfs/build.sh installs keycard-init at this path and prints this same
    # cmdline; a mismatch is a microVM that boots to nothing.
    assert "init=/usr/sbin/keycard-init" in DEFAULT_BOOT_ARGS
    assert "console=ttyS0" in DEFAULT_BOOT_ARGS


# -- reaping what a crash left behind ----------------------------------------
#
# A graceful exit clears up after itself; a SIGKILL or a power cut does not,
# and a paused room's directory holds a plaintext dump of the guest's RAM.
# These run anywhere — no /dev/kvm, no firecracker binary.


def _instance_dir(tmp_path: Path, name: str, *, owner: str = "", vmm: str = "") -> Path:
    path = tmp_path / name
    path.mkdir()
    (path / "memory").write_bytes(b"pretend guest RAM")
    if owner:
        (path / "owner.pid").write_text(owner)
    if vmm:
        (path / "vmm.pid").write_text(vmm)
    return path


@pytest.mark.skipif(not Path("/proc/self").exists(), reason="needs /proc")
def test_a_directory_whose_owners_are_both_gone_is_debris(tmp_path: Path) -> None:
    # pid 0 is never a real process, so both owners read as dead.
    path = _instance_dir(tmp_path, "ubuntu-dead", owner="0", vmm="0")
    assert fc.instance_is_debris(path) is True


@pytest.mark.skipif(not Path("/proc/self").exists(), reason="needs /proc")
def test_a_live_keycard_owner_protects_a_half_built_room(tmp_path: Path) -> None:
    """The window between `mkdir` and a VMM existing is real, and a concurrent
    keycard sweeping through it must not take a room that is still opening."""
    import os

    path = _instance_dir(tmp_path, "ubuntu-building", owner=str(os.getpid()))
    assert fc.instance_is_debris(path) is False


@pytest.mark.skipif(not Path("/proc/self").exists(), reason="needs /proc")
def test_a_live_vmm_protects_a_running_room(tmp_path: Path) -> None:
    """If keycard was killed but its firecracker survived, the room is still
    running — removing the directory would pull the rootfs out from under a
    live guest. This test process stands in for the VMM, so the argv it is
    matched against is the one actually running it."""
    import os
    import sys

    path = _instance_dir(tmp_path, "ubuntu-orphaned", owner="0", vmm=str(os.getpid()))
    stand_in = Path(sys.executable).name
    assert fc._process_is(path / "vmm.pid", stand_in) is True


@pytest.mark.skipif(not Path("/proc/self").exists(), reason="needs /proc")
def test_a_recycled_vmm_pid_does_not_pass_for_firecracker(tmp_path: Path) -> None:
    """A pid alone is not evidence, and the VMM check is where that bites:
    this process is alive and holds the recorded number, but it is not
    firecracker, so the real VMM is gone and the directory really is debris."""
    import os

    path = _instance_dir(tmp_path, "ubuntu-recycled", owner="0", vmm=str(os.getpid()))
    assert fc._process_is(path / "vmm.pid", "firecracker") is False
    assert fc.instance_is_debris(path) is True


@pytest.mark.skipif(not Path("/proc/self").exists(), reason="needs /proc")
def test_a_live_owner_is_not_required_to_be_named_keycard(tmp_path: Path) -> None:
    """The owner check must not insist on a command line.

    `python -m keycard`, a wrapper script or a supervisor all leave a live
    keycard whose argv says something else, and reaping a running room's
    directory is a far worse failure than leaving debris for the next sweep.
    This test process is exactly such a case — alive, and not named keycard.
    """
    import os

    path = _instance_dir(tmp_path, "ubuntu-oddly-named", owner=str(os.getpid()))
    assert fc._process_is(path / "owner.pid", "keycard") is False
    assert fc.instance_is_debris(path) is False


def test_an_unreadable_pid_file_counts_as_dead(tmp_path: Path) -> None:
    """A directory with no pid files at all predates this bookkeeping, or was
    interrupted before writing one. Either way nothing is using it."""
    path = _instance_dir(tmp_path, "ubuntu-nopids")
    assert fc.instance_is_debris(path) is True

    garbage = _instance_dir(tmp_path, "ubuntu-garbage", owner="not-a-pid", vmm="")
    assert fc.instance_is_debris(garbage) is True


@pytest.mark.skipif(not Path("/proc/self").exists(), reason="needs /proc")
def test_reap_debris_removes_only_the_dead(tmp_path: Path) -> None:
    import os

    dead = _instance_dir(tmp_path, "ubuntu-dead", owner="0", vmm="0")
    live = _instance_dir(tmp_path, "ubuntu-live", owner=str(os.getpid()))

    reaped = fc.reap_debris(tmp_path)

    assert reaped == ["ubuntu-dead"]
    assert not dead.exists()
    # And the RAM dump went with it, which is the entire point.
    assert not (dead / "memory").exists()
    assert live.exists()


def test_reap_debris_on_a_missing_runtime_dir_is_a_noop(tmp_path: Path) -> None:
    assert fc.reap_debris(tmp_path / "never-created") == []


# -- --keep is refused ------------------------------------------------------


def test_the_backend_refuses_to_pause() -> None:
    """No `--keep` on a microVM.

    A Firecracker snapshot writes the whole of guest RAM to a file the VMM
    later restores by reading it directly, so it cannot be made secret at
    rest without still being plaintext at resume. Refused rather than
    shipped with a caveat — see `FirecrackerRoom.can_pause`.
    """
    assert fc.FirecrackerRoom.can_pause is False


def test_room_and_backend_agree_about_pausing() -> None:
    """The two declarations must not drift.

    `Room.can_pause` is what actually gates the pause at teardown;
    `Backend.can_pause` is what lets `serve()` warn at boot, before any room
    exists. If they disagree the server either promises a reconnect window it
    will not honour, or warns about one it would have given.
    """
    assert fc.FirecrackerRoom.can_pause is fc.FirecrackerBackend.can_pause


def test_pause_is_kept_even_though_nothing_calls_it() -> None:
    """Deliberately not deleted along with the feature.

    A host upgraded from 0.2.x can still hold snapshots an older keycard
    wrote, and the startup sweep is what reaps them — both that sweep and the
    tests proving it works need a way to produce a paused room.
    """
    assert callable(fc.FirecrackerRoom.pause)
    assert callable(fc.FirecrackerBackend.destroy_kept)
