"""
Settings shared across image builders, the installer and VM tests.
"""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path

# -- identity ---------------------------------------------------------------

#: The ISO's volume label, and the CDLABEL the live cmdline searches for.
VOLID = "UBUNTU-UKI-ISO"

#: The FAT label on the target's EFI System Partition.
#:
#: Capped at 11 characters by FAT, and mkfs.vfat fails outright rather than
#: truncating — so an over-long name breaks the build at the last step. The
#: length is asserted before use.
ESP_LABEL = "UBUNTU-UKI"

#: GPT partition name and filesystem label for the target's root partition.
#: The installed UKI selects this partition with root=PARTLABEL=.
ROOT_LABEL = "ubuntu-uki-iso-root"

#: The hostname and the label of the firmware boot entry.
BOOT_LABEL = "xiahualab"

#: Architecture of the built kernel packages and root filesystems.
DPKG_ARCH = "amd64"

#: Installed root filesystem carried by the LiveUSB.
PAYLOAD_SQUASHFS = Path("payload/base.squashfs")


class Mode(StrEnum):
    """
    Image and boot-package variants shared by builders and the installer.
    """

    LIVE = "live"
    INSTALLED = "installed"
