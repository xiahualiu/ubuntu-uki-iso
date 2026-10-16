"""
Disk discovery uses the supplied hardware geometry and refuses ambiguous selections.
"""

import json
import subprocess
from typing import Any
from unittest.mock import Mock

import pytest

from ubuntu_uki_iso.errors import Refusal
from ubuntu_uki_iso.installer import device, discovery, preflight, raid
from ubuntu_uki_iso.installer.context import Context
from ubuntu_uki_iso.installer.preflight import Preflight, RaidMode


def hardware() -> list[dict[str, object]]:
    """
    Represent the user's nominal 1 TB NVMe and eight nominal 8 TB HDDs.
    """
    return [
        {
            "name": "/dev/nvme0n1",
            "type": "disk",
            "size": 1_000_204_886_016,
            "rota": False,
            "rm": False,
            "ro": False,
        },
        *[
            {
                "name": f"/dev/sd{name}",
                "type": "disk",
                "size": 8_001_563_222_016,
                "rota": True,
                "rm": False,
                "ro": False,
            }
            for name in "abcdefgh"
        ],
    ]


def mock_inventory(monkeypatch: pytest.MonkeyPatch, rows: list[dict[str, object]]) -> None:
    def run(argv: list[Any], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        assert argv[0] == "lsblk" and "--bytes" in argv and "--nodeps" in argv
        assert kwargs["check"] is True
        return subprocess.CompletedProcess(argv, 0, json.dumps({"blockdevices": rows}), "")

    monkeypatch.setattr(subprocess, "run", run)


def test_discovers_supplied_geometry_without_selecting_partitions_or_arrays(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rows = hardware()
    rows += [
        {**rows[0], "name": "/dev/loop0", "type": "loop"},
        {**rows[0], "name": "/dev/nvme0n1p2", "type": "part"},
        {**rows[1], "name": "/dev/md0", "type": "raid0"},
        {**rows[0], "name": "/dev/usb", "rm": True},
        {**rows[0], "name": "/dev/readonly", "ro": True},
    ]
    mock_inventory(monkeypatch, rows)
    root, data = discovery.select(None, [], manage_data=True, expected_devices=8)
    assert root == "/dev/nvme0n1"
    assert data == [f"/dev/sd{name}" for name in "abcdefgh"]


@pytest.mark.parametrize("extra_root", [False, True])
def test_refuses_missing_or_ambiguous_root(
    monkeypatch: pytest.MonkeyPatch, extra_root: bool
) -> None:
    rows = hardware()
    if extra_root:
        rows.append({**rows[0], "name": "/dev/nvme1n1"})
    else:
        rows = rows[1:]
    mock_inventory(monkeypatch, rows)
    with pytest.raises(Refusal, match="expected one"):
        discovery.select(None, [], manage_data=True, expected_devices=8)


@pytest.mark.parametrize("count", [7, 9])
def test_requires_exactly_eight_matching_hdds(monkeypatch: pytest.MonkeyPatch, count: int) -> None:
    rows = hardware()[:8] if count == 7 else hardware()
    if count == 9:
        rows.append({**rows[1], "name": "/dev/sdi"})
    mock_inventory(monkeypatch, rows)
    with pytest.raises(Refusal, match="expected 8 HDDs"):
        discovery.select(None, [], manage_data=True, expected_devices=8)


def test_explicit_devices_do_not_need_capacity_discovery(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        discovery, "inventory", Mock(side_effect=AssertionError("unexpected discovery"))
    )
    assert discovery.select(
        "/dev/test", ["/dev/a", "/dev/b"], manage_data=True, expected_devices=2
    ) == ("/dev/test", ["/dev/a", "/dev/b"])


def test_raid_none_needs_only_a_root_candidate(monkeypatch: pytest.MonkeyPatch) -> None:
    mock_inventory(monkeypatch, hardware()[:1])
    assert discovery.select(None, [], manage_data=False, expected_devices=8) == ("/dev/nvme0n1", [])


def test_mounted_root_is_refused_before_any_formatting(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(device, "exists", lambda disk: True)
    monkeypatch.setattr(device, "examine_md", lambda disk: None)
    monkeypatch.setattr(device, "is_rotational", lambda disk: False)
    monkeypatch.setattr(preflight, "_children", lambda disk: [disk + "p1", disk + "p2"])
    monkeypatch.setattr(device, "is_mounted", lambda disk: disk.endswith("p1"))
    monkeypatch.setattr(subprocess, "run", Mock(side_effect=AssertionError("unexpected command")))
    with pytest.raises(Refusal, match="currently mounted"):
        preflight.check_root_disk("/dev/nvme0n1")


def test_existing_mounted_array_is_left_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    ctx = Context(
        "/dev/nvme0n1",
        [f"/dev/sd{name}" for name in "abcdefgh"],
        RaidMode.AUTO,
        dry_run=False,
        confirmed=True,
    )
    monkeypatch.setattr(device, "exists", lambda disk: True)
    monkeypatch.setattr(device, "active_array", lambda: "md0")
    monkeypatch.setattr(device, "settle_udev", Mock())
    monkeypatch.setattr(device, "is_mounted", lambda disk: True)
    monkeypatch.setattr(device, "uuid_of", lambda disk: "existing-filesystem-uuid")
    monkeypatch.setattr(raid, "_verify_shape", Mock())
    commands = Mock(side_effect=AssertionError("existing mounted array must not be modified"))
    monkeypatch.setattr(subprocess, "run", commands)
    raid._reuse(ctx, Preflight([], active_array="md0"))
    assert ctx.data_uuid == "existing-filesystem-uuid"
    commands.assert_not_called()


def test_partition_table_prevents_treating_a_disk_as_blank(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda argv, **kwargs: subprocess.CompletedProcess(argv, 0, "PTTYPE=gpt\n", ""),
    )
    assert device.filesystems("/dev/sda") == ("partition-table:gpt",)


def test_failed_signature_probe_is_not_treated_as_blank(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda argv, **kwargs: subprocess.CompletedProcess(argv, 4, "", "permission denied"),
    )
    with pytest.raises(Refusal, match="could not inspect signatures"):
        device.filesystems("/dev/sda")


def test_unreadable_raid_metadata_is_not_treated_as_blank(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda argv, **kwargs: subprocess.CompletedProcess(
            argv, 1, "", "cannot open device: permission denied"
        ),
    )
    with pytest.raises(Refusal, match="could not examine RAID metadata"):
        device.examine_md("/dev/sda")
