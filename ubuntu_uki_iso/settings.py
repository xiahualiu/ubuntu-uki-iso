"""Build settings.

Everything here is a value that could reasonably differ between machines or
between runs. Each one can be overridden from the environment with an
``UBUNTU_UKI_ISO_`` prefix, which is how CI passes them in and how a one-off build
changes them without editing files.

What is *not* here: anything that would change the meaning of the image. The
volume label is the interesting case — it appears both in the live kernel
command line and in the ISO's metadata, and if those two disagree the ISO boots
to an initramfs rescue shell with no explanation of why. It is defined once,
here, and the cmdline template interpolates it.
"""

from __future__ import annotations

import os

ENV_PREFIX = "UBUNTU_UKI_ISO_"


def _env(name: str, default: str) -> str:
    return os.environ.get(ENV_PREFIX + name, default)


# -- identity ---------------------------------------------------------------

#: The ISO's volume label, and the CDLABEL the live cmdline searches for.
VOLID = _env("VOLID", "UBUNTU-UKI-ISO")

#: The FAT label on the target's EFI System Partition.
#:
#: Capped at 11 characters by FAT, and mkfs.vfat fails outright rather than
#: truncating — so an over-long name breaks the build at the last step. The
#: length is asserted before use.
ESP_LABEL = _env("ESP_LABEL", "UBUNTU-UKI")

#: Label for the root filesystem on the target.
ROOT_LABEL = _env("ROOT_LABEL", "ubuntu-uki-iso-root")

#: The GPT partition GUID the installer always writes for the root partition.
#:
#: Fixed rather than generated, because it is what the installed command line
#: names: ``root=PARTUUID=`` is baked into the UKI, and that UKI is built here,
#: before the partition exists. So the identity of a partition that does not
#: exist yet has to be a constant both sides read.
#:
#: Derived as a UUIDv5 of the project URL rather than invented, so it is
#: reproducible and obviously not random. It is deliberately the *same* on
#: every machine this ISO provisions — which is fine while a machine has one
#: root disk, but would make ``root=PARTUUID=`` ambiguous if a second disk
#: provisioned from this ISO were ever attached to the same machine. The mount
#: identity does not have this problem: /etc/fstab keeps the filesystem UUID
#: mkfs generates, so only the boot path is duplicated.
ROOT_PARTUUID = _env("ROOT_PARTUUID", "a0228cdd-e4e1-5447-91d6-305a7b2c0b5a")

#: The hostname and the label of the firmware boot entry.
BOOT_LABEL = _env("BOOT_LABEL", "xiahualab")

#: The kernel-install entry token, written to /etc/kernel/entry-token.
#:
#: It decides the UKI's filename. Without it kernel-install falls back to the
#: machine-id — which the target does not have until it first boots, and which
#: this build machine has a different value of. A fixed token is what lets the
#: UKI built here carry the same name every later build on the target will
#: give its own.
ENTRY_TOKEN = _env("ENTRY_TOKEN", BOOT_LABEL)

#: The path firmware boots with no configuration at all.
#:
#: Writing the current UKI here is what lets the machine boot without anyone
#: opening the firmware setup menu, and — because the installer formats the
#: ESP — it is also the safety net if the NVRAM entry is lost or the board
#: ignores it.
FALLBACK_EFI_PATH = r"\EFI\BOOT\BOOTX64.EFI"

#: Where the ESP is mounted on the installed system.
#:
#: Two things have to agree on this and they are written at different times: the
#: fstab mounts the partition here, and the kernel package's postinst hook
#: points kernel-install's boot root here. A disagreement puts the UKI
#: somewhere the firmware never looks.
ESP_MOUNT = _env("ESP_MOUNT", "/boot/efi")

# -- the UKI package --------------------------------------------------------

#: The architecture stamped into the UKI package, and the one every image in
#: this project is built for. There is exactly one target.
DPKG_ARCH = _env("DPKG_ARCH", "amd64")

#: The name of the package that carries the target's kernel, modules and UKI.
#:
#: Deliberately not a ``linux-image-*`` name: the kernel's own maintainer
#: scripts key off that prefix, and this package is not a kernel image — it
#: carries no vmlinuz the kernel would recognise and no modules its scripts
#: installed.
PACKAGE_NAME = _env("PACKAGE_NAME", "ubuntu-uki-iso-uki")

#: The Maintainer field of the UKI package. Required by dpkg-deb, and the
#: package is never uploaded to an archive, so this is the project's own
#: identity rather than a person's address.
PACKAGE_MAINTAINER = _env("PACKAGE_MAINTAINER", "xiahualiu <xiahualiu@users.noreply.github.com>")

# -- distributions ----------------------------------------------------------

UBUNTU_SUITE = _env("UBUNTU_SUITE", "resolute")
UBUNTU_MIRROR = _env("UBUNTU_MIRROR", "http://archive.ubuntu.com/ubuntu")
UBUNTU_COMPONENTS = _env("UBUNTU_COMPONENTS", "main,universe")

#: The Ubuntu kernel *series*. Note this is not the kernel release:
#: linux-source-7.0.0 carries upstream 7.0.14, and the built release is
#: whatever that source tree's Makefile says plus CONFIG_LOCALVERSION. The
#: release is never assumed — it is read back from the source tree.
KERNEL_SOURCE_PKG = _env("KERNEL_SOURCE_PKG", "linux-source-7.0.0")

#: Which Ubuntu kernel config to start the trim from. /boot/config-<ver> ships
#: inside linux-modules-<ver>, not the image package.
BASE_KERNEL = _env("BASE_KERNEL", "7.0.0-31-generic")

#: Appended to the kernel release via CONFIG_LOCALVERSION.
LOCALVERSION = _env("LOCALVERSION", "-ubuntu-uki-iso")

# -- container --------------------------------------------------------------

BUILD_IMAGE = _env("BUILD_IMAGE", "ubuntu-uki-iso-build:latest")
TEST_IMAGE = _env("TEST_IMAGE", "ubuntu-uki-iso-test:latest")
DOCKER = _env("DOCKER", "docker")

# -- the data array ---------------------------------------------------------

#: The existing array's identity. The installer verifies what it finds against
#: these before assembling, and refuses if anything disagrees — a RAID0
#: assembled with the wrong geometry still mounts and silently returns
#: different bytes past the first missing chunk.
MD_DEVICE = _env("MD_DEVICE", "/dev/md0")
EXPECTED_MD_UUID = _env("EXPECTED_MD_UUID", "3955b9de:69b7772b:e7cd882f:92edf554")
EXPECTED_MD_LEVEL = _env("EXPECTED_MD_LEVEL", "raid0")
EXPECTED_MD_DEVICES = int(_env("EXPECTED_MD_DEVICES", "8"))
EXPECTED_MD_CHUNK = _env("EXPECTED_MD_CHUNK", "512K")
RAID_METADATA_VERSION = _env("RAID_METADATA_VERSION", "1.2")

#: How many UKIs to keep on the ESP. 1 GB holds roughly 10-15 at ~50 MB each.
UKI_RETENTION = int(_env("UKI_RETENTION", "3"))

# -- test -------------------------------------------------------------------

VM_MEMORY_MB = int(_env("VM_MEMORY_MB", "4096"))
VM_CPUS = int(_env("VM_CPUS", "4"))
VM_BOOT_TIMEOUT = int(_env("VM_BOOT_TIMEOUT", "900"))
VM_COMMAND_TIMEOUT = int(_env("VM_COMMAND_TIMEOUT", "1800"))
VM_ROOT_DISK_SIZE = _env("VM_ROOT_DISK_SIZE", "12G")
VM_DATA_DISK_SIZE = _env("VM_DATA_DISK_SIZE", "2G")

#: How many data disks the QEMU harness emulates.
#:
#: Two, standing in for the real machine's eight. The disks are on AHCI —
#: the same controller the real ones use — and QEMU's emulated ICH9 AHCI has
#: six ports, so eight do not fit on one controller. Two controllers are
#: possible but make the guest's device names depend on PCI probe order, which
#: is the kind of flakiness that reads as an installer bug.
#:
#: Disk *count* is the cheap thing to give up here. The decision table's
#: combinatorial cases — partial membership, split arrays, wrong member counts
#: — are covered exhaustively by the pytest suite against constructed device
#: state. What the VM run adds is proof that the plumbing works on a real
#: kernel: that an array assembles, that the reuse path leaves the bytes alone,
#: and that the refusal fires. Two disks prove all three.
#:
#: The harness passes this through as UBUNTU_UKI_ISO_EXPECTED_MD_DEVICES, so the
#: installer is told the shape of the array it should find rather than
#: assuming it.
VM_DATA_DISKS = int(_env("VM_DATA_DISKS", "2"))

#: QEMU's emulated ICH9 AHCI port count. Not a setting — a fact about the
#: emulation, kept here so the harness can refuse a configuration that cannot
#: work rather than failing inside QEMU with "Bus 'ahci.6' not found".
AHCI_PORTS = 6
