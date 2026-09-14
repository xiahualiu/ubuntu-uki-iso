"""Reading a UKI's sections.

The shell version of this project reached for ``objcopy``, which meant binutils
had to be present wherever the check ran — including on the installed system,
where it is not. These tests build PE files by hand, so the parser is exercised
without needing a 50 MB UKI.
"""

from __future__ import annotations

import struct

import pytest

from helpers import make_pe, make_uki
from ubuntu_uki_iso import pe


@pytest.fixture
def uki(tmp_path):
    path = tmp_path / "BOOTX64.EFI"
    path.write_bytes(make_uki(b"root=UUID=abc ro quiet"))
    return path


def test_reads_the_cmdline_section(uki):
    assert pe.cmdline(uki) == "root=UUID=abc ro quiet"


def test_reads_a_binary_section(uki):
    assert pe.read_section(uki, ".initrd").startswith(b"initramfs")


def test_a_missing_section_is_none_not_an_error(uki):
    """'This UKI has no command line of its own' is an answer, not a failure."""
    assert pe.read_section(uki, ".nope") is None


def test_has_section_distinguishes_a_uki_from_a_bootloader(tmp_path):
    loader = tmp_path / "shimx64.efi"
    loader.write_bytes(make_pe({".text": b"\x90" * 64}))
    assert pe.has_section(loader) is False


def test_a_non_pe_file_is_handled(tmp_path):
    not_pe = tmp_path / "random.bin"
    not_pe.write_bytes(b"this is not a PE file at all, not even close" * 10)
    assert pe.has_section(not_pe) is False
    with pytest.raises(pe.NotPE):
        pe.read_section(not_pe)


def test_a_truncated_file_is_handled(tmp_path):
    truncated = tmp_path / "short.bin"
    truncated.write_bytes(b"MZ\x00\x00")
    with pytest.raises(pe.NotPE):
        pe.read_section(truncated)


def test_an_implausible_section_count_is_rejected(tmp_path):
    """A corrupt header must not turn into a huge allocation."""
    data = bytearray(make_pe({".cmdline": b"x"}))
    struct.pack_into("<H", data, 0x40 + 4 + 2, 5000)  # NumberOfSections
    path = tmp_path / "corrupt.efi"
    path.write_bytes(bytes(data))
    with pytest.raises(pe.NotPE):
        pe.read_section(path)


def test_the_real_uki_is_parsed_if_one_has_been_built():
    """Opportunistic: exercises the parser against a genuine 50 MB UKI.

    Skipped on a fresh checkout, because building one takes a kernel.
    """
    from ubuntu_uki_iso.paths import Layout

    uki_file = Layout().uki_file
    if not uki_file.is_file():
        pytest.skip("no UKI built yet")

    embedded = pe.cmdline(uki_file)
    assert embedded, "a UKI with no command line is not a UKI"
