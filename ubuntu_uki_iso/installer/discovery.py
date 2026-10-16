"""
Select whole disks by nominal capacity without modifying any devices.
"""

import json
import subprocess
from dataclasses import dataclass

from ..errors import Refusal
from . import config


@dataclass(frozen=True)
class Disk:
    """
    Physical-disk attributes reported by lsblk using exact byte counts.
    """

    name: str
    size: int
    rotational: bool
    removable: bool
    readonly: bool


def inventory() -> list[Disk]:
    """
    Read whole disks only, excluding partitions, loop devices and assembled arrays.
    """
    result = subprocess.run(
        [
            "lsblk",
            "--json",
            "--bytes",
            "--paths",
            "--nodeps",
            "--output",
            "NAME,TYPE,SIZE,ROTA,RM,RO",
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=120,
    )
    try:
        devices = json.loads(result.stdout)["blockdevices"]
        disks = [
            Disk(
                str(item["name"]),
                int(item["size"]),
                bool(int(item["rota"])),
                bool(int(item["rm"])),
                bool(int(item["ro"])),
            )
            for item in devices
            if item["type"] == "disk"
        ]
    except (KeyError, ValueError, TypeError) as exc:
        raise Refusal(f"could not read the lsblk disk inventory: {exc}") from exc
    return sorted(disks, key=lambda disk: disk.name)


def select(
    root_disk: str | None,
    data_disks: list[str],
    *,
    manage_data: bool,
    expected_devices: int,
) -> tuple[str, list[str]]:
    """
    Discover missing disk arguments; preserve explicit choices for other hardware.

    Capacities use decimal TB: a nominal 1 TB drive is displayed as about 931.5G
    by lsblk, and a nominal 8 TB drive as about 7.3T. Ambiguous roots or incomplete
    sets of data drives are refused before any partitioning or formatting.
    """
    if expected_devices < 1:
        raise Refusal("--expected-md-devices must be positive")
    disks = inventory() if not root_disk or (manage_data and not data_disks) else []
    eligible = [disk for disk in disks if not disk.removable and not disk.readonly]
    if not root_disk:
        roots = [
            disk.name
            for disk in eligible
            if not disk.rotational
            and config.ROOT_DISK_MIN_BYTES <= disk.size <= config.ROOT_DISK_MAX_BYTES
        ]
        if len(roots) != 1:
            raise Refusal(
                f"expected one non-removable SSD around 1 TB, found {len(roots)}: "
                f"{', '.join(roots) or 'none'}. Select the root disk explicitly with --root-disk."
            )
        root_disk = roots[0]
    if manage_data and not data_disks:
        data_disks = [
            disk.name
            for disk in eligible
            if disk.rotational
            and disk.name != root_disk
            and config.DATA_DISK_MIN_BYTES <= disk.size <= config.DATA_DISK_MAX_BYTES
        ]
        if len(data_disks) != expected_devices:
            raise Refusal(
                f"expected {expected_devices} HDDs around 8 TB, found {len(data_disks)}: "
                f"{', '.join(data_disks) or 'none'}. No disks have been changed."
            )
    if not manage_data:
        data_disks = []
    if len(data_disks) != len(set(data_disks)):
        raise Refusal("--data-disks contains duplicate device names")
    return root_disk, data_disks
