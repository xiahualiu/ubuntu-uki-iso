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

from .. import settings
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

#: The control field the UKI package records its kernel release in.
#:
#: Separate from ``Version`` on purpose: a repackaging that does not rebuild the
#: kernel has to change Version, while the release — which names
#: ``/lib/modules/<release>`` and the UKI's filename — must not.
RELEASE_FIELD = "X-Kernel-Release"


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
    def uki_package(self) -> Path | None:
        """The UKI package among those found on the medium."""
        for deb in self.debs:
            if deb.name.startswith(settings.PACKAGE_NAME):
                return deb
        return None

    def load_release(self) -> str:
        """The kernel release the package on the medium will install.

        Read from the package's own control field rather than from its file name
        or from a directory listing: the name is a convention, the field is what
        dpkg records. The payload carries neither a kernel nor a UKI, so this
        package is the only authority on which kernel this machine is about to
        run and which UKI belongs on its ESP.

        The field is then checked against the modules the package actually
        ships. It is the one place the two could disagree, and a disagreement
        means the UKI and the modules on the machine would be for different
        kernels — which is a machine that boots and cannot load a driver.
        """
        package = self.uki_package
        if package is None:
            return ""

        release = self.runner.capture("dpkg-deb", "-f", str(package), RELEASE_FIELD).strip()
        if not release:
            raise BuildError(
                f"{package.name} carries no {RELEASE_FIELD} field.\n"
                "Without it there is no way to know which kernel this machine is about\n"
                "to run, or which UKI to expect on its ESP."
            )

        listing = self.runner.capture("dpkg-deb", "--contents", str(package))
        if f"lib/modules/{release}" not in listing:
            raise BuildError(
                f"{package.name} says it is for kernel {release}, but ships no\n"
                f"/lib/modules/{release}.\n"
                "Its metadata and its payload disagree, so the package is malformed:\n"
                "the UKI would boot a kernel with no modules to load."
            )

        self.release = release
        return self.release


@dataclass
class StepResult:
    """What a step reports back, for the closing summary."""

    name: str
    detail: str = ""
    notes: list[str] = field(default_factory=list)
