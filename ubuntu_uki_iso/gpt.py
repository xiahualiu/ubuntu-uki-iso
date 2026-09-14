"""Parsing an ISO's partition table, to prove it is actually hybrid.

This exists because of a specific, silent failure. The flag combination the
design notes named for a UEFI-only hybrid ISO — ``-e efi.img -no-emul-boot
-isohybrid-gpt-basdat`` — produces no MBR and no GPT at all. The resulting ISO
has a perfectly good El Torito UEFI entry, boots fine from a CD or from
``qemu -cdrom``, and is completely invisible to firmware when written to a USB
stick. Nothing about the build reports a problem.

So the build parses what it produced and refuses to hand over an image that
would only work on one of the two kinds of medium.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from pathlib import Path

#: EFI System Partition.
ESP_TYPE_GUID = "C12A7328-F81F-11D2-BA4B-00A0C93EC93B"

_MBR_SIGNATURE = b"\x55\xaa"
_GPT_SIGNATURE = b"EFI PART"
_MAX_ENTRIES = 512  # the spec allows 128 by default; this only bounds a corrupt header


@dataclass(frozen=True)
class Partition:
    type_guid: str
    first_lba: int
    last_lba: int
    name: str

    @property
    def size_bytes(self) -> int:
        return (self.last_lba - self.first_lba + 1) * 512

    @property
    def size_mb(self) -> int:
        return self.size_bytes // (1024 * 1024)


@dataclass(frozen=True)
class PartitionTable:
    has_mbr: bool
    partitions: tuple[Partition, ...]

    @property
    def has_gpt(self) -> bool:
        return bool(self.partitions)

    def esp(self) -> Partition | None:
        for partition in self.partitions:
            if partition.type_guid == ESP_TYPE_GUID:
                return partition
        return None


def _format_guid(raw: bytes) -> str:
    """Mixed-endian, as the GPT spec specifies. Little-endian in three fields,
    big-endian in the last two — not a mistake, just an old format."""
    first, second, third = struct.unpack("<IHH", raw[0:8])
    return (
        f"{first:08X}-{second:04X}-{third:04X}-{raw[8:10].hex().upper()}-{raw[10:16].hex().upper()}"
    )


def parse(path: Path) -> PartitionTable:
    """Read the MBR signature and, if present, the GPT partition entries.

    Never raises for a malformed image — an ISO with no partition table is a
    legitimate thing to be handed, and the caller decides what it means.
    """
    with open(path, "rb") as handle:
        head = handle.read(1024)

        has_mbr = len(head) >= 512 and head[510:512] == _MBR_SIGNATURE
        if len(head) < 1024 or head[512:520] != _GPT_SIGNATURE:
            return PartitionTable(has_mbr, ())

        entry_lba, count, entry_size = struct.unpack_from("<QII", head, 512 + 72)
        if not (0 < count <= _MAX_ENTRIES) or entry_size < 128:
            return PartitionTable(has_mbr, ())

        handle.seek(entry_lba * 512)
        table = handle.read(count * entry_size)

    partitions = []
    for index in range(count):
        entry = table[index * entry_size : (index + 1) * entry_size]
        if len(entry) < 128 or entry[:16] == b"\0" * 16:
            continue
        first, last = struct.unpack_from("<QQ", entry, 32)
        name = entry[56:128].decode("utf-16-le", errors="replace").rstrip("\0")
        partitions.append(Partition(_format_guid(entry[:16]), first, last, name))

    return PartitionTable(has_mbr, tuple(partitions))


def parse_el_torito(text: str) -> bool:
    """Whether xorriso's El Torito report contains a bootable UEFI entry.

    Takes the text of ``xorriso -report_el_torito plain``. The line looks like::

        El Torito boot img :   1  UEFI  y   none  0x0000  0x00      0    35

    so this looks for the platform and the bootable flag, in that order and on
    the same line, rather than for the substring "efi" — which also matches the
    image path ``/boot/efi.img`` and would pass on an image with no UEFI entry
    at all.
    """
    for line in text.splitlines():
        if "UEFI" not in line:
            continue
        tail = line.split("UEFI", 1)[1].split()
        if tail and tail[0].lower() == "y":
            return True
    return False
