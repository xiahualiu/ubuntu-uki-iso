"""The RAID decision table.

This is the part of the project that can destroy 58 TB, so it is tested against
constructed device state rather than against whatever disks happen to be
attached. Every case below is a scenario someone could actually hit, and the
refusals are asserted to *explain themselves* — a refusal that does not is one
an operator learns to work around.
"""

from __future__ import annotations

import pytest

from ubuntu_uki_iso.installer.device import MdSuperblock
from ubuntu_uki_iso.installer.preflight import (
    Decision,
    DiskScan,
    Preflight,
    RaidMode,
    decide,
)

MD_UUID = "3955b9de:69b7772b:e7cd882f:92edf554"
OTHER_UUID = "cafebabe:cafebabe:cafebabe:cafebabe"

DATA_DISKS = [f"/dev/sd{c}" for c in "abcdefgh"]
ROOT_DISK = "/dev/nvme0n1"


def member(level: str = "raid0", devices: int = 8, uuid: str = MD_UUID) -> MdSuperblock:
    return MdSuperblock(level=level, devices=devices, uuid=uuid, metadata="1.2", chunk="512K")


def scan_all(
    md: MdSuperblock | None = None, filesystems: tuple[str, ...] = (), mounted: bool = False
) -> Preflight:
    """Every data disk in the same state."""
    scans = [
        DiskScan(
            device=path,
            exists=True,
            mounted=mounted,
            md=md,
            filesystems=filesystems,
        )
        for path in DATA_DISKS
    ]
    uuids = [md.uuid] if md else []
    return Preflight(scans=scans, array_uuids=uuids)


def verdict(preflight: Preflight, mode: RaidMode) -> tuple[Decision, str]:
    decide(preflight, ROOT_DISK, mode)
    return preflight.decision, preflight.refusal


# ---------------------------------------------------------------------------
# The happy path
# ---------------------------------------------------------------------------


def test_all_members_present_is_reused():
    decision, _ = verdict(scan_all(md=member()), RaidMode.REUSE)
    assert decision is Decision.REUSE


def test_reuse_wins_over_an_explicit_create():
    """--raid=create does not override an array that is actually there.

    Reusing is the safe reading of an ambiguous ask: the operator who wanted a
    new array also wanted to erase eight disks first, and that is a separate
    deliberate act.
    """
    decision, _ = verdict(scan_all(md=member()), RaidMode.CREATE)
    assert decision is Decision.REUSE


# ---------------------------------------------------------------------------
# The refusal that matters most
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("mode", [RaidMode.REUSE, RaidMode.CREATE])
def test_filesystems_without_a_superblock_are_refused_in_both_modes(mode):
    """Disks with data but no md superblock.

    This is the catastrophic case, and there is no flag that makes it fine —
    which is why it is refused under --raid=create too.
    """
    decision, reason = verdict(scan_all(filesystems=("ext4",)), mode)
    assert decision is Decision.REFUSE
    assert "NOT blank" in reason


def test_blank_disks_are_not_created_without_being_asked():
    decision, reason = verdict(scan_all(), RaidMode.REUSE)
    assert decision is Decision.REFUSE
    assert "No md superblock" in reason
    assert "Nothing has been changed" in reason


def test_blank_disks_are_created_when_asked():
    decision, _ = verdict(scan_all(), RaidMode.CREATE)
    assert decision is Decision.CREATE


# ---------------------------------------------------------------------------
# Wrong or mixed arrays
# ---------------------------------------------------------------------------


def test_a_different_array_uuid_is_refused():
    decision, reason = verdict(scan_all(md=member(uuid=OTHER_UUID)), RaidMode.REUSE)
    assert decision is Decision.REFUSE
    assert OTHER_UUID in reason and MD_UUID in reason


def test_partial_membership_is_refused():
    scans = [
        DiskScan(path, True, False, member() if i < 4 else None, ())
        for i, path in enumerate(DATA_DISKS)
    ]
    decision, reason = verdict(Preflight(scans=scans, array_uuids=[MD_UUID]), RaidMode.REUSE)
    assert decision is Decision.REFUSE
    assert "partial" in reason.lower()


def test_two_arrays_on_one_set_of_disks_is_refused():
    scans = [
        DiskScan(path, True, False, member(uuid=MD_UUID if i < 4 else OTHER_UUID), ())
        for i, path in enumerate(DATA_DISKS)
    ]
    decision, reason = verdict(
        Preflight(scans=scans, array_uuids=[MD_UUID, OTHER_UUID]), RaidMode.REUSE
    )
    assert decision is Decision.REFUSE
    assert "different array UUIDs" in reason


# ---------------------------------------------------------------------------
# Physical impossibilities
# ---------------------------------------------------------------------------


def test_a_missing_disk_is_refused():
    scans = [DiskScan(path, path != "/dev/sde", False, member(), ()) for path in DATA_DISKS]
    decision, reason = verdict(Preflight(scans=scans, array_uuids=[MD_UUID]), RaidMode.REUSE)
    assert decision is Decision.REFUSE
    assert "/dev/sde" in reason


def test_a_mounted_member_is_refused():
    decision, reason = verdict(scan_all(md=member(), mounted=True), RaidMode.REUSE)
    assert decision is Decision.REFUSE
    assert "mounted" in reason


def test_the_root_disk_may_not_also_be_a_data_disk():
    scans = [DiskScan(path, True, False, member(), ()) for path in DATA_DISKS]
    scans[0] = DiskScan(ROOT_DISK, True, False, member(), ())
    disks = [ROOT_DISK, *DATA_DISKS[1:]]
    preflight = Preflight(scans=scans, array_uuids=[MD_UUID])
    decide(preflight, ROOT_DISK, RaidMode.REUSE, expected_devices=8)
    # The overlap check runs before the shape checks, so this refuses for the
    # right reason rather than incidentally.
    assert preflight.decision is Decision.REFUSE
    assert "both the root disk and a data disk" in preflight.refusal
    assert disks  # silence the unused-variable lint without changing the case


# ---------------------------------------------------------------------------
# Shape mismatch
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("md", "fragment"),
    [
        (member(level="raid5"), "level"),
        (member(devices=7), "member slots"),
    ],
)
def test_a_differently_shaped_array_is_refused(md, fragment):
    """A RAID0 that assembles with the wrong geometry still mounts.

    It just returns different bytes at every offset, which is why the shape is
    verified rather than assumed.
    """
    decision, reason = verdict(scan_all(md=md), RaidMode.REUSE)
    assert decision is Decision.REFUSE
    assert fragment in reason


def test_raid_none_skips_the_array_entirely():
    decision, _ = verdict(scan_all(), RaidMode.NONE)
    assert decision is Decision.NONE


def test_raid_mode_rejects_an_unknown_value():
    from ubuntu_uki_iso.errors import Refusal

    with pytest.raises(Refusal):
        RaidMode.parse("destroy-everything")
