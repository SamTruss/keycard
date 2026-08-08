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


# -- boot args --------------------------------------------------------------


def test_default_boot_args_name_the_init_the_rootfs_actually_ships() -> None:
    # rootfs/build.sh installs keycard-init at this path and prints this same
    # cmdline; a mismatch is a microVM that boots to nothing.
    assert "init=/usr/sbin/keycard-init" in DEFAULT_BOOT_ARGS
    assert "console=ttyS0" in DEFAULT_BOOT_ARGS
