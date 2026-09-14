"""Just enough PE parsing to read a UKI's embedded command line.

A UKI is a PE binary whose interesting parts are sections: ``.linux`` is the
kernel, ``.initrd`` the initramfs, ``.cmdline`` the kernel command line.

The shell version of this project read ``.cmdline`` back out with ``objcopy``,
which meant binutils had to be present wherever the check ran — including on
the installed system, where it is not. Parsing four fields out of a header
removes that dependency and makes the check testable without a real UKI.

This is not a general PE library. It reads section headers and nothing else,
and it refuses anything it does not recognise rather than guessing.
"""

from __future__ import annotations

import struct
from pathlib import Path

#: Section name as it appears in the header, NUL-padded to eight bytes.
CMDLINE_SECTION = ".cmdline"

_PE_SIGNATURE = b"PE\0\0"
_SECTION_HEADER_SIZE = 40
_MAX_SECTIONS = 96  # real UKIs have a handful; this only bounds a corrupt header


class NotPE(Exception):
    """The file is not a PE binary, or its headers are not readable."""


def _section_table_offset(data: bytes) -> tuple[int, int]:
    """Return ``(table_offset, section_count)``."""
    if len(data) < 0x40:
        raise NotPE("file is too short to contain a DOS header")

    (pe_offset,) = struct.unpack_from("<I", data, 0x3C)
    if pe_offset + 24 > len(data):
        raise NotPE(f"PE header offset {pe_offset} is past the end of the file")
    if data[pe_offset : pe_offset + 4] != _PE_SIGNATURE:
        raise NotPE("no PE signature — this is not a PE binary")

    # COFF header follows the signature: NumberOfSections is at +2, and
    # SizeOfOptionalHeader at +16. The section table sits after the optional
    # header, whose length varies by architecture and subsystem.
    (section_count,) = struct.unpack_from("<H", data, pe_offset + 6)
    (optional_size,) = struct.unpack_from("<H", data, pe_offset + 20)

    if section_count > _MAX_SECTIONS:
        raise NotPE(f"implausible section count {section_count}")

    table = pe_offset + 24 + optional_size
    if table + section_count * _SECTION_HEADER_SIZE > len(data):
        raise NotPE("section table runs past the end of the file")
    return table, section_count


def read_section(path: Path, name: str = CMDLINE_SECTION) -> bytes | None:
    """The raw bytes of a PE section, or ``None`` if there is no such section.

    ``None`` rather than an exception for a missing section: "this UKI has no
    command line of its own" is a real answer, not a parse failure.
    """
    data = path.read_bytes()
    table, count = _section_table_offset(data)

    for index in range(count):
        entry = table + index * _SECTION_HEADER_SIZE
        raw_name = data[entry : entry + 8]
        if raw_name.rstrip(b"\0") != name.encode():
            continue

        size, offset = struct.unpack_from("<II", data, entry + 16)
        if size == 0:
            return b""
        if offset + size > len(data):
            raise NotPE(f"section {name} claims {size} bytes at {offset}, past end of file")
        return data[offset : offset + size]

    return None


def has_section(path: Path, name: str = CMDLINE_SECTION) -> bool:
    """Whether the file carries a section — used to tell a UKI from a loader."""
    try:
        return read_section(path, name) is not None
    except (NotPE, OSError):
        return False


def cmdline(path: Path) -> str | None:
    """A UKI's embedded kernel command line as text.

    Strips NUL padding, which ukify uses to round the section up.
    """
    raw = read_section(path, CMDLINE_SECTION)
    if raw is None:
        return None
    return raw.decode("utf-8", errors="replace").strip("\0").strip()
