"""State shared across the installer's steps.

One object rather than a parameter list, because the steps genuinely share
this and threading eight arguments through five functions is how they drift
apart. It also makes the dry run legible: every field here is either an input
the operator gave or something a step discovered, and the fields that are only
filled in when actually running are marked as such.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from ..errors import BuildError
from ..log import Console
from ..paths import Layout
from ..proc import Runner
from . import device
from .preflight import RaidMode

#: Where the target root filesystem is mounted while it is being built.
TARGET_MOUNT = Path("/mnt/ubuntu-uki-iso-target")

#: Where the existing array is mounted, read-only, to prove it is readable.
RAID_VERIFY_MOUNT = Path("/mnt/ubuntu-uki-iso-raid-check")

#: The kernel image package's name prefix. What follows it is the kernel
#: release, which is the string every other part of the system keys off.
IMAGE_PACKAGE_PREFIX = "linux-image-"


@dataclass
class Context:
    root_disk: str
    data_disks: list[str]
    runner: Runner
    console: Console
    layout: Layout
    mode: RaidMode
    esp_size: str = "1G"
    md_device: str = "/dev/md0"
    payload: Path | None = None
    #: The kernel packages found on the medium, beside the payload. Empty only
    #: in a dry run whose medium was not found.
    debs: list[Path] = field(default_factory=list)
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

    @property
    def image_deb(self) -> Path | None:
        """The kernel image package among those found on the medium."""
        for deb in self.debs:
            if deb.name.startswith(IMAGE_PACKAGE_PREFIX):
                return deb
        return None

    def load_release(self) -> str:
        """The kernel release the packages on the medium will install.

        Read out of the image package's control metadata rather than from its
        file name or from a directory listing. The name is a convention
        bindeb-pkg happens to follow; ``Package:`` is what dpkg records and what
        the installed system will answer to. The payload carries no kernel any
        more, so the package is the only authority on which kernel this machine
        is about to run.
        """
        image = self.image_deb
        if image is None:
            return ""

        package = self.runner.capture("dpkg-deb", "-f", str(image), "Package")
        if not package.startswith(IMAGE_PACKAGE_PREFIX):
            raise BuildError(
                f"{image.name} is not a kernel image package: it calls itself {package!r}.\n"
                f"The release is what follows {IMAGE_PACKAGE_PREFIX!r}, so it cannot be derived."
            )
        self.release = package[len(IMAGE_PACKAGE_PREFIX) :]
        return self.release


@dataclass
class StepResult:
    """What a step reports back, for the closing summary."""

    name: str
    detail: str = ""
    notes: list[str] = field(default_factory=list)
