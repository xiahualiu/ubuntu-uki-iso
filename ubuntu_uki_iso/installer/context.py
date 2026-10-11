"""State shared across the installer's steps.

One object rather than a parameter list, because the steps genuinely share
this and threading eight arguments through five functions is how they drift
apart. It also makes the dry run legible: every field here is either an input
the operator gave or something a step discovered, and the fields that are only
filled in when actually running are marked as such.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ..log import Console
from ..proc import Runner
from . import device
from .preflight import RaidMode

#: Where the target root filesystem is mounted while it is being built.
TARGET_MOUNT = Path("/mnt/ubuntu-uki-iso-target")

#: Where the existing array is mounted, read-only, to prove it is readable.
RAID_VERIFY_MOUNT = Path("/mnt/ubuntu-uki-iso-raid-check")


@dataclass
class Context:
    root_disk: str
    data_disks: list[str]
    runner: Runner
    console: Console
    mode: RaidMode
    esp_size: str = "1G"
    md_device: str = "/dev/md0"
    payload: Path | None = None
    source_uki: Path | None = None
    force_root_disk: bool = False

    # -- filled in by the steps --------------------------------------------
    part_esp: str = ""
    part_root: str = ""
    root_uuid: str = ""
    esp_uuid: str = ""
    release: str = ""
    uki_path: Path | None = None
    uki_rel: str = ""
    #: Filesystem UUID of the data array, for fstab.
    data_uuid: str = ""

    @property
    def target(self) -> Path:
        return TARGET_MOUNT

    @property
    def esp_mount(self) -> Path:
        return TARGET_MOUNT / "boot/efi"

    @property
    def dry_run(self) -> bool:
        return self.runner.dry_run

    def derive_partitions(self) -> None:
        """Compute the partition device names from the root disk.

        Only valid once the disk is known, and only *used* once partitioning
        has happened — but computed up front so the dry run can name them.
        """
        self.part_esp = device.part_name(self.root_disk, 1)
        self.part_root = device.part_name(self.root_disk, 2)
