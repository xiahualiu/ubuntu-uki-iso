"""Fixed project settings. Edit these values here to change the configuration."""

from __future__ import annotations

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

#: Fixed GPT partition GUID retained for reproducible installer partitioning.
#: Boot selection uses ROOT_LABEL rather than this GUID.
ROOT_PARTUUID = "a0228cdd-e4e1-5447-91d6-305a7b2c0b5a"

#: The hostname and the label of the firmware boot entry.
BOOT_LABEL = "xiahualab"

#: The path firmware boots with no configuration at all.
#:
#: Writing the current UKI here is what lets the machine boot without anyone
#: opening the firmware setup menu, and — because the installer formats the
#: ESP — it is also the safety net if the NVRAM entry is lost or the board
#: ignores it.
FALLBACK_EFI_PATH = r"\EFI\BOOT\BOOTX64.EFI"

#: Where the ESP is mounted on the installed system.
ESP_MOUNT = "/boot/efi"

#: Architecture of the built kernel packages and root filesystems.
DPKG_ARCH = "amd64"

# -- distributions ----------------------------------------------------------

UBUNTU_SUITE = "resolute"
UBUNTU_MIRROR = "http://archive.ubuntu.com/ubuntu"
UBUNTU_COMPONENTS = "main,universe"

#: The Ubuntu kernel *series*. Note this is not the kernel release:
#: linux-source-7.0.0 carries upstream 7.0.14, and the built release is
#: whatever that source tree's Makefile says plus CONFIG_LOCALVERSION. The
#: release is never assumed — it is read back from the source tree.
KERNEL_SOURCE_PKG = "linux-source-7.0.0"

#: Appended to the kernel release via CONFIG_LOCALVERSION.
LOCALVERSION = "-ubuntu-uki-iso"

# -- the data array ---------------------------------------------------------

#: The existing array's identity. The installer verifies what it finds against
#: these before assembling, and refuses if anything disagrees — a RAID0
#: assembled with the wrong geometry still mounts and silently returns
#: different bytes past the first missing chunk.
MD_DEVICE = "/dev/md0"
EXPECTED_MD_UUID = "3955b9de:69b7772b:e7cd882f:92edf554"
EXPECTED_MD_LEVEL = "raid0"
EXPECTED_MD_DEVICES = 8
EXPECTED_MD_CHUNK = "512K"
RAID_METADATA_VERSION = "1.2"

# -- test -------------------------------------------------------------------

VM_MEMORY_MB = 4096
VM_CPUS = 4
VM_BOOT_TIMEOUT = 900
VM_COMMAND_TIMEOUT = 1800
VM_ROOT_DISK_SIZE = "12G"
VM_DATA_DISK_SIZE = "2G"

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
#: The harness passes this through as --expected-md-devices, so the
#: installer is told the shape of the array it should find rather than
#: assuming it.
VM_DATA_DISKS = 2

#: QEMU's emulated ICH9 AHCI port count. Not a setting — a fact about the
#: emulation, kept here so the harness can refuse a configuration that cannot
#: work rather than failing inside QEMU with "Bus 'ahci.6' not found".
AHCI_PORTS = 6
