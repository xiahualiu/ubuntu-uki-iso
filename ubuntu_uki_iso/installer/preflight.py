"""The RAID guardrails.

This is the part of the project that can destroy 58 TB, so it is written to be
read rather than to be clever. The rules, in order of how much they matter:

1. Creating an array requires ``--raid=create``. Nothing infers it. Not an
   empty disk, not a missing array, not a flag that "looks like" intent.
2. Creating an array is refused if *any* target disk carries a filesystem
   signature or an md superblock. A disk that has ever held data is not a blank
   disk, and this installer does not get to decide it is expendable.
3. The reuse path never writes a superblock. No ``--create``, no
   ``--zero-superblock``, no ``--update``, no ``--force``.
4. A partial set of member disks is a refusal, not a repair opportunity. Some
   disks having superblocks and some not means something happened here that
   this code does not understand.
5. Anything unrecognised stops the run and explains itself. There is no
   "continue anyway" flag, because the operator who needs one at 2am is exactly
   the operator who should not have one.

The refusals are paragraphs on purpose. A refusal that does not explain itself
teaches the operator to look for a flag that turns the check off, and there
isn't one.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

from .. import settings
from ..errors import Refusal
from ..log import Console
from ..proc import Runner
from . import device


class Decision(Enum):
    REUSE = "reuse"
    CREATE = "create"
    NONE = "none"
    REFUSE = "refuse"


class RaidMode(Enum):
    REUSE = "reuse"
    CREATE = "create"
    NONE = "none"

    @classmethod
    def parse(cls, value: str) -> RaidMode:
        try:
            return cls(value)
        except ValueError:
            raise Refusal(f"--raid must be reuse, create or none (got {value!r})") from None


@dataclass
class DiskScan:
    """What one data disk looks like, without having touched it."""

    device: str
    exists: bool
    mounted: bool
    md: device.MdSuperblock | None
    filesystems: tuple[str, ...]

    @property
    def is_blank(self) -> bool:
        """Nothing recognisable on it at all."""
        return self.md is None and not self.filesystems

    def state(self) -> str:
        if not self.exists:
            return "MISSING"
        if self.md is not None:
            state = f"md member ({self.md.level}, uuid {self.md.uuid})"
        elif self.filesystems:
            state = f"filesystem: {', '.join(self.filesystems)}"
        else:
            state = "blank"
        return f"{state}, MOUNTED" if self.mounted else state


@dataclass
class Preflight:
    scans: list[DiskScan]
    decision: Decision = Decision.REFUSE
    refusal: str = ""
    active_array: str | None = None
    #: Distinct array UUIDs seen, in the order encountered.
    array_uuids: list[str] = field(default_factory=list)

    @property
    def members(self) -> list[DiskScan]:
        return [scan for scan in self.scans if scan.md is not None]

    @property
    def with_filesystems(self) -> list[DiskScan]:
        return [scan for scan in self.scans if scan.filesystems]

    @property
    def missing(self) -> list[DiskScan]:
        return [scan for scan in self.scans if not scan.exists]

    @property
    def mounted(self) -> list[DiskScan]:
        return [scan for scan in self.scans if scan.mounted]

    @property
    def blank(self) -> list[DiskScan]:
        return [scan for scan in self.scans if scan.exists and scan.is_blank]


# ---------------------------------------------------------------------------
# Scan
# ---------------------------------------------------------------------------


def scan(runner: Runner, data_disks: list[str]) -> Preflight:
    """Inspect every data disk. Read-only, and always runs."""
    scans = []
    for path in data_disks:
        if not device.exists(path):
            scans.append(DiskScan(path, False, False, None, ()))
            continue
        scans.append(
            DiskScan(
                device=path,
                exists=True,
                mounted=device.is_mounted(runner, path),
                md=device.examine_md(runner, path),
                filesystems=device.filesystems(runner, path),
            )
        )

    seen: list[str] = []
    for scan_ in scans:
        if scan_.md and scan_.md.uuid not in seen:
            seen.append(scan_.md.uuid)

    return Preflight(scans=scans, array_uuids=seen, active_array=device.active_array(runner))


def report(preflight: Preflight, runner: Runner, root_disk: str, console: Console) -> None:
    console.step("preflight: scanning (read-only)")
    console.info(f"{'DEVICE':<16} {'SIZE':<10} STATE")
    console.info("-" * 62)

    for scan_ in preflight.scans:
        size = device.human_size(runner, scan_.device) if scan_.exists else "-"
        console.info(f"{scan_.device:<16} {size:<10} {scan_.state()}")

    console.info("")
    if device.exists(root_disk):
        console.info(f"root disk: {root_disk} ({device.human_size(runner, root_disk)})")
    else:
        console.info(f"root disk: {root_disk} — DOES NOT EXIST")

    if preflight.active_array:
        console.grey(f"already-active array: /dev/{preflight.active_array}")


# ---------------------------------------------------------------------------
# Decide
# ---------------------------------------------------------------------------


def decide(
    preflight: Preflight,
    root_disk: str,
    mode: RaidMode,
    *,
    expected_uuid: str | None = None,
    expected_level: str | None = None,
    expected_devices: int | None = None,
) -> Preflight:
    """Apply the decision table.

    Sets ``decision`` and, on refusal, ``refusal`` to a paragraph explaining
    why. Never raises — the caller decides what a refusal means, because the
    dry run wants to print it and the apply run wants to exit 2.
    """
    expected_uuid = expected_uuid or settings.EXPECTED_MD_UUID
    expected_level = expected_level or settings.EXPECTED_MD_LEVEL
    expected_devices = (
        expected_devices if expected_devices is not None else settings.EXPECTED_MD_DEVICES
    )

    def refuse(reason: str) -> Preflight:
        preflight.decision = Decision.REFUSE
        preflight.refusal = reason
        return preflight

    if mode is RaidMode.NONE:
        preflight.decision = Decision.NONE
        return preflight

    # --- every named disk must exist --------------------------------------
    if preflight.missing:
        names = ", ".join(scan.device for scan in preflight.missing)
        total = len(preflight.scans)
        missing = len(preflight.missing)
        headline = (
            f"One of the {total} data disks is missing"
            if missing == 1
            else f"{missing} of the {total} data disks are missing"
        )
        return refuse(
            f"These data disks do not exist: {names}.\n"
            "\n"
            f"Refusing to continue. {headline}, and a missing disk is a hardware\n"
            "problem to look at rather than something to work around. Creating an\n"
            "array across whatever is left would silently redefine what this machine's\n"
            "storage is — a different geometry, and therefore different data at every\n"
            "offset."
        )

    # --- nothing may be mounted -------------------------------------------
    if preflight.mounted:
        names = ", ".join(scan.device for scan in preflight.mounted)
        return refuse(
            f"These data disks are mounted: {names}.\n"
            "\n"
            "Refusing to continue while they are in use. Unmount them first — and if\n"
            "you did not expect them to be mounted, find out what mounted them before\n"
            "you do anything else."
        )

    # --- the root disk is not a data disk ---------------------------------
    for scan_ in preflight.scans:
        if scan_.device == root_disk:
            return refuse(
                f"{scan_.device} was given as both the root disk and a data disk.\n"
                "That is never what anyone means."
            )

    present = [scan for scan in preflight.scans if scan.exists]
    members = preflight.members

    # --- all members present ----------------------------------------------
    if len(members) == len(present) and present:
        if len(preflight.array_uuids) != 1:
            listing = "\n".join(f"      {uuid}" for uuid in preflight.array_uuids)
            return refuse(
                f"The data disks carry {len(preflight.array_uuids)} different array UUIDs:\n"
                f"{listing}\n"
                "\n"
                "These disks are not members of one array. Refusing to guess which set is\n"
                "the real one — assembling across them would mix data from two arrays and\n"
                "produce something that reads as corruption."
            )

        found = preflight.array_uuids[0]
        if found != expected_uuid:
            return refuse(
                f"The array on these disks has UUID {found},\n"
                f"but this installer expects {expected_uuid}.\n"
                "\n"
                "Refusing to assemble. Assembling the wrong array would mount the wrong\n"
                "data — and with --raid=create it would mean overwriting an array this\n"
                "code has never heard of."
            )

        level = members[0].md.level if members[0].md else ""
        if level and level != expected_level:
            return refuse(
                f"The array is level {level}; this installer expects {expected_level}.\n"
                "Refusing to assemble an array whose shape it does not understand."
            )

        count = members[0].md.devices if members[0].md else None
        if count is not None and count != expected_devices:
            return refuse(
                f"The array has {count} member slots; this installer expects "
                f"{expected_devices}.\n"
                "Refusing to assemble — a degraded or resized RAID0 is not a degraded\n"
                "array, it is a different array with different data at every offset."
            )

        preflight.decision = Decision.REUSE
        return preflight

    # --- no members at all -------------------------------------------------
    if not members:
        # Filesystems on the target disks. This is the catastrophic case, and
        # it is refused under BOTH modes — there is no flag that makes it fine.
        if preflight.with_filesystems:
            names = ", ".join(scan.device for scan in preflight.with_filesystems)
            flag_note = "The --raid=create flag was given. " if mode is RaidMode.CREATE else ""
            return refuse(
                f"These data disks carry filesystems: {names}.\n"
                "\n"
                "No md superblock was found, but these disks are NOT blank. Refusing to\n"
                "touch them under any mode, including --raid=create.\n"
                "\n"
                f"{flag_note}If these disks really are expendable, erase them yourself\n"
                "first and re-run — the erasure should be a deliberate act, not a side\n"
                "effect of an installer."
            )

        if mode is RaidMode.CREATE:
            preflight.decision = Decision.CREATE
            return preflight

        example = present[0].device if present else "/dev/sda"
        return refuse(
            "No md superblock was found on any data disk, and --raid=create was not\n"
            "given (or --raid=none was).\n"
            "\n"
            "Nothing has been changed. Three things could be true here:\n"
            "\n"
            "  * The array exists but is not visible. Check that all eight disks are\n"
            "    present and that mdadm can read them:\n"
            f"        mdadm --examine {example}\n"
            "\n"
            "  * You meant to build a new array. That is opt-in, because it destroys\n"
            "    whatever is on those disks:\n"
            "        ubuntu-uki-iso install --apply --raid=create ...\n"
            "\n"
            "  * You did not mean to run this here at all. Nothing was written, so\n"
            "    nothing needs undoing."
        )

    # --- partial membership ------------------------------------------------
    member_names = "\n".join(f"      {scan.device}" for scan in members)
    other_names = ", ".join(
        scan.device for scan in preflight.scans if scan.exists and scan.md is None
    )
    return refuse(
        f"{len(members)} of {len(present)} data disks carry an md superblock:\n"
        f"{member_names}\n"
        "\n"
        f"but the rest do not: {other_names}\n"
        "\n"
        "A partial member set means something happened here that this installer does\n"
        "not understand — a disk was replaced, wiped, or added. Refusing to assemble\n"
        "or create. Work out which disks belong to the array before going further."
    )


def enforce(preflight: Preflight, console: Console) -> None:
    """Turn a refusal into an exception."""
    if preflight.decision is Decision.REFUSE:
        raise Refusal(preflight.refusal)


# ---------------------------------------------------------------------------
# Root disk
# ---------------------------------------------------------------------------


def check_root_disk(runner: Runner, root_disk: str, *, force: bool = False) -> None:
    """Sanity-check the disk that is about to be repartitioned.

    The single most valuable check here after the array rules: an operator
    typing ``/dev/sda`` instead of ``/dev/nvme0n1`` would otherwise
    repartition one of the eight spinners.
    """
    if not device.exists(root_disk):
        raise Refusal(f"the root disk {root_disk} does not exist")

    if md := device.examine_md(runner, root_disk):
        raise Refusal(
            f"{root_disk} carries an md superblock ({md.level}, uuid {md.uuid}), so it is\n"
            "(or was) a member of an array. It cannot also be the root disk.\n"
            "This looks like a data disk was named by mistake."
        )

    if device.is_rotational(root_disk) and not force:
        raise Refusal(
            f"{root_disk} is a rotational disk.\n"
            "\n"
            "The root disk on this machine is the NVMe (Samsung 980 PRO). A rotational\n"
            "disk here is almost certainly one of the eight 8 TB data disks, and\n"
            "repartitioning it would destroy data.\n"
            "\n"
            "If you really mean it, pass --force-root-disk."
        )

    # Anything mounted is in use.
    candidates = [root_disk, *_children(runner, root_disk)]
    for path in candidates:
        if device.is_mounted(runner, path):
            raise Refusal(
                f"{path} (on {root_disk}) is currently mounted.\n"
                "Refusing to repartition a disk that is in use."
            )

    # A disk holding the running root filesystem is not one to partition.
    running_source = runner.capture("findmnt", "-n", "-o", "SOURCE", "/", check=False)
    if running_source and Path(running_source).name.startswith(Path(root_disk).name):
        raise Refusal(
            f"{root_disk} holds the running root filesystem.\n"
            "This installer is meant to run from the live ISO, against a disk that "
            "is not in use."
        )


def _children(runner: Runner, disk: str) -> list[str]:
    """Partitions of a disk, as full device paths."""
    result = runner.probe("lsblk", "-nlo", "NAME", disk, check=False)
    lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    return [f"/dev/{name}" for name in lines[1:]]  # the first line is the disk itself
