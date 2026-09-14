"""Looking at disks without touching them.

Everything here is read-only. That is not a convention to be maintained by
discipline — it is why the functions take a :class:`~ubuntu_uki_iso.proc.Runner`
and call ``probe``, which executes unconditionally even during a dry run.

``mdadm --examine --export`` is used in preference to the human-readable
``--examine`` output because it emits ``KEY=VALUE`` lines. Parsing the table
means parsing column alignment and optional fields; parsing ``KEY=VALUE`` means
splitting on the first equals sign.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ..proc import Runner

#: `mdadm --examine --export` keys we care about.
_LEVEL_KEY = "MD_LEVEL"
_DEVICES_KEY = "MD_DEVICES"
_UUID_KEY = "MD_UUID"
_METADATA_KEY = "MD_METADATA"
_CHUNK_KEY = "MD_CHUNK_SIZE"


@dataclass(frozen=True)
class MdSuperblock:
    """What ``mdadm --examine`` can tell us about a member disk."""

    level: str
    devices: int | None
    uuid: str
    metadata: str
    chunk: str

    def describe(self) -> str:
        return f"{self.level}, {self.devices} devices, {self.metadata} metadata, uuid {self.uuid}"


def part_name(disk: str, number: int) -> str:
    """``/dev/nvme0n1`` -> ``/dev/nvme0n1p1``, but ``/dev/sda`` -> ``/dev/sda1``."""
    if disk.startswith(("/dev/nvme", "/dev/mmcblk", "/dev/loop")):
        return f"{disk}p{number}"
    return f"{disk}{number}"


def exists(device: str) -> bool:
    return Path(device).is_block_device()


def size_bytes(runner: Runner, device: str) -> int:
    raw = runner.capture("blockdev", "--getsize64", device, check=False)
    try:
        return int(raw)
    except ValueError:
        return 0


def human_size(runner: Runner, device: str) -> str:
    total = size_bytes(runner, device)
    value = float(total)
    for unit in ("B", "K", "M", "G", "T"):
        if value < 1024:
            return f"{value:.0f}{unit}"
        value /= 1024
    return f"{value:.0f}P"


def is_rotational(device: str) -> bool:
    """Whether the kernel considers this spinning rust.

    Used as a guard, not a fact: on this machine a rotational root disk is
    almost certainly one of the eight 8 TB data disks, and repartitioning one
    of those by mistake is the disaster the whole installer is arranged around.
    """
    resolved = Path(device).resolve().name
    if resolved.startswith(("nvme", "mmcblk", "vd", "loop")):
        return False
    try:
        return Path(f"/sys/block/{resolved}/queue/rotational").read_text().strip() == "1"
    except OSError:
        return False


def is_mounted(runner: Runner, device: str) -> bool:
    return runner.probe("findmnt", "-n", "-S", device, check=False).ok


def filesystems(runner: Runner, device: str) -> tuple[str, ...]:
    """Filesystem signatures on a device.

    Empty means "looks unused", which is the only condition under which this
    installer will create anything.
    """
    result = runner.probe("blkid", "-p", "-o", "value", "-s", "TYPE", device, check=False)
    return tuple(sorted({line.strip() for line in result.stdout.splitlines() if line.strip()}))


def uuid_of(runner: Runner, device: str) -> str:
    return runner.capture("blkid", "-s", "UUID", "-o", "value", device, check=False).strip()


def examine_md(runner: Runner, device: str) -> MdSuperblock | None:
    """The md superblock on a device, or ``None`` if there is not one.

    ``--export`` needs no privileges to *fail*, which is what happens when the
    device carries no superblock — so a nonzero exit is the ordinary "no"
    answer, not an error.
    """
    result = runner.probe("mdadm", "--examine", "--export", device, check=False)
    if not result.ok and not result.stdout.strip():
        return None

    values: dict[str, str] = {}
    for line in result.stdout.splitlines():
        key, separator, value = line.partition("=")
        if separator:
            values[key.strip()] = value.strip()

    uuid = values.get(_UUID_KEY, "")
    if not uuid:
        return None

    devices: int | None = None
    if raw := values.get(_DEVICES_KEY):
        try:
            devices = int(raw)
        except ValueError:
            devices = None

    return MdSuperblock(
        level=values.get(_LEVEL_KEY, ""),
        devices=devices,
        uuid=uuid,
        metadata=values.get(_METADATA_KEY, ""),
        chunk=values.get(_CHUNK_KEY, ""),
    )


def active_array(runner: Runner) -> str | None:
    """The first running md array, e.g. ``md0``.

    Read from ``/proc/mdstat`` rather than ``mdadm --detail --scan`` because it
    needs no privileges and cannot itself cause an assembly.
    """
    try:
        text = Path("/proc/mdstat").read_text()
    except OSError:
        return None
    for line in text.splitlines():
        name = line.split(" ", 1)[0]
        if name.startswith("md") and name[2:].isdigit() and " : " in line:
            return name
    return None


def settle_udev(runner: Runner, timeout: int = 30) -> None:
    """Wait for udev, after partitioning.

    Without this, mkfs races the kernel's partition rescan and intermittently
    fails on a device node that does not exist yet — a failure that reproduces
    about one run in ten.
    """
    if runner.have("udevadm"):
        runner.run("udevadm", "settle", f"--timeout={timeout}", check=False)
