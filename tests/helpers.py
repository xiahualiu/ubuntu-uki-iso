"""Builders for the binary structures the tests parse.

Constructing these by hand, rather than checking in fixtures, means the tests
document the format they depend on — and a fixture that silently stops matching
the real thing is a class of test failure that never announces itself.
"""

from __future__ import annotations

import struct

#: EFI System Partition, as it appears in a GPT entry.
ESP_TYPE_GUID = bytes.fromhex("28732ac11ff8d211ba4b00a0c93ec93b")
BASIC_DATA_TYPE_GUID = bytes.fromhex("a2a0d0ebe5b9334487c068b6b72699c7")


def make_pe(sections: dict[str, bytes], *, optional_size: int = 0xF0) -> bytes:
    """A minimal but structurally valid PE binary.

    Only the fields the parser reads are meaningful; everything else is zero.
    """
    names = list(sections)
    section_table_offset = 4 + 20 + optional_size

    headers = bytearray(0x40)
    headers[0:2] = b"MZ"
    struct.pack_into("<I", headers, 0x3C, 0x40)  # e_lfanew

    coff = bytearray()
    coff += b"PE\0\0"
    coff += struct.pack("<HHIIIHH", 0x8664, len(names), 0, 0, 0, optional_size, 0x0022)

    optional = bytes(optional_size)

    table_size = len(names) * 40
    data_offset = (0x40 + section_table_offset + table_size + 0xFF) & ~0xFF

    table = bytearray()
    blobs = bytearray()
    for name in names:
        blob = sections[name]
        cursor = data_offset + len(blobs)
        table += name.encode()[:8].ljust(8, b"\0")
        table += struct.pack("<IIII", 0, 0, len(blob), cursor)
        table += bytes(16)
        blobs += blob

    padding = bytes(data_offset - (0x40 + section_table_offset + table_size))
    return bytes(headers) + bytes(coff) + optional + bytes(table) + padding + bytes(blobs)


def make_uki(cmdline: bytes = b"root=UUID=abc ro quiet") -> bytes:
    """A UKI-shaped PE: kernel, initramfs, and an embedded command line."""
    return make_pe(
        {
            ".linux": b"\x7fELF" + b"kernel" * 64,
            ".initrd": b"initramfs" * 64,
            ".cmdline": cmdline.ljust(64, b"\0"),
        }
    )


def make_gpt_iso(
    *,
    total_bytes: int = 4 * 1024 * 1024,
    partitions: list[tuple[bytes, int, int, str]] | None = None,
    mbr: bool = True,
    gpt: bool = True,
    unique_guids: list[bytes] | None = None,
) -> bytes:
    """An ISO-shaped file with an MBR signature and/or a GPT.

    ``partitions`` is a list of ``(type_guid_bytes, first_lba, last_lba, name)``,
    and ``unique_guids`` the per-partition GUIDs in the same order — raw bytes,
    because the on-disk encoding is mixed-endian and building one from a string
    is the thing under test rather than something to be assumed here.
    """
    partitions = partitions if partitions is not None else [(ESP_TYPE_GUID, 64, 2048, "Appended2")]
    image = bytearray(total_bytes)

    if mbr:
        image[510:512] = b"\x55\xaa"

    if gpt:
        image[512:520] = b"EFI PART"
        entry_lba, entry_count, entry_size = 2, 128, 128
        # GPT header: entry array LBA at +72, count at +80, size at +84
        struct.pack_into("<QII", image, 512 + 72, entry_lba, entry_count, entry_size)

        base = entry_lba * 512
        for index, (type_guid, first, last, name) in enumerate(partitions):
            entry = bytearray(entry_size)
            entry[0:16] = type_guid
            if unique_guids is not None:
                entry[16:32] = unique_guids[index]
            else:
                entry[16:32] = bytes(16)
            struct.pack_into("<QQ", entry, 32, first, last)
            encoded = name.encode("utf-16-le")
            entry[56 : 56 + len(encoded)] = encoded
            offset = base + index * entry_size
            image[offset : offset + entry_size] = entry

    return bytes(image)


def el_torito_report(platform: str = "UEFI", bootable: str = "y") -> str:
    """The shape of ``xorriso -report_el_torito plain`` output."""
    return (
        "El Torito catalog  : 34  1\n"
        "El Torito cat path : /boot.catalog\n"
        "El Torito images   :   N  Pltf  B   Emul  Ld_seg  Hdpt  Ldsiz         LBA\n"
        f"El Torito boot img :   1  {platform}  {bootable}   none  0x0000  0x00"
        "      0          35\n"
        "El Torito img path :   1  /boot/efi.img\n"
    )
