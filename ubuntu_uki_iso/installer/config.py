"""
Target partition identity and EFI filesystem layout.
"""

#: Fixed GPT partition GUID retained for reproducible installer partitioning.
#: Boot selection uses ROOT_LABEL rather than this GUID.
ROOT_PARTUUID = "a0228cdd-e4e1-5447-91d6-305a7b2c0b5a"

#: The path firmware boots with no configuration at all.
#:
#: Writing the current UKI here is what lets the machine boot without anyone
#: opening the firmware setup menu, and — because the installer formats the
#: ESP — it is also the safety net if the NVRAM entry is lost or the board
#: ignores it.
FALLBACK_EFI_PATH = r"\EFI\BOOT\BOOTX64.EFI"

#: Where the ESP is mounted on the installed system.
ESP_MOUNT = "/boot/efi"


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


#: Match the existing md0 ext4 mount policy. The array is optional at boot;
#: stop waiting for its filesystem device after 10 seconds if it is absent.
DATA_MOUNT_OPTIONS = (
    "defaults,noatime,nodiratime,data=writeback,barrier=0,commit=600,"
    "nofail,x-systemd.device-timeout=10s"
)


#: Nominal capacities in decimal bytes, allowing 10 percent variation.
ROOT_DISK_MIN_BYTES = 900_000_000_000
ROOT_DISK_MAX_BYTES = 1_100_000_000_000
DATA_DISK_MIN_BYTES = 7_200_000_000_000
DATA_DISK_MAX_BYTES = 8_800_000_000_000


#: Public key preloaded into the new installed account; empty disables preloading.
SSH_PUBLIC_KEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIFXMOd2BGLse5HOADKAOSEMd4Y806swHHFDdink7Rro1"
