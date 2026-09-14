"""Proving an ISO is actually hybrid.

These checks exist because of a specific silent failure: the flag combination
the design named produces a valid El Torito entry and no partition table at
all. Such an ISO boots from a CD, boots in QEMU with ``-cdrom``, and is a
coaster on a USB stick — with nothing in the build output saying so.
"""

from __future__ import annotations

import pytest

from helpers import BASIC_DATA_TYPE_GUID, ESP_TYPE_GUID, el_torito_report, make_gpt_iso
from ubuntu_uki_iso import gpt


@pytest.fixture
def iso(tmp_path):
    def write(**kwargs):
        path = tmp_path / "test.iso"
        path.write_bytes(make_gpt_iso(**kwargs))
        return path

    return write


# ---------------------------------------------------------------------------
# Partition table
# ---------------------------------------------------------------------------


def test_a_correct_hybrid_is_recognised(iso):
    table = gpt.parse(iso())
    assert table.has_mbr
    assert table.has_gpt
    esp = table.esp()
    assert esp is not None
    assert esp.name == "Appended2"


def test_the_designs_original_flags_are_caught(iso):
    """No MBR and no GPT — which is what `-isohybrid-gpt-basdat` alone gives."""
    table = gpt.parse(iso(mbr=False, gpt=False))
    assert not table.has_mbr
    assert not table.has_gpt
    assert table.esp() is None


def test_an_mbr_without_a_gpt_is_distinguishable(iso):
    table = gpt.parse(iso(gpt=False))
    assert table.has_mbr
    assert not table.has_gpt


def test_a_gpt_without_an_esp_type_is_caught(iso):
    """The partition is there but firmware will not recognise it as bootable."""
    table = gpt.parse(iso(partitions=[(BASIC_DATA_TYPE_GUID, 64, 2048, "Gap0")]))
    assert table.has_gpt
    assert table.esp() is None


def test_the_esp_is_found_among_other_partitions(iso):
    table = gpt.parse(
        iso(
            partitions=[
                (BASIC_DATA_TYPE_GUID, 64, 128, "Gap0"),
                (ESP_TYPE_GUID, 129, 131200, "Appended2"),
                (BASIC_DATA_TYPE_GUID, 131201, 131328, "Gap1"),
            ]
        )
    )
    esp = table.esp()
    assert esp is not None
    assert esp.name == "Appended2"
    assert esp.size_mb == 64


def test_an_empty_gpt_entry_is_skipped(iso):
    table = gpt.parse(iso(partitions=[(bytes(16), 0, 0, "")]))
    assert table.esp() is None


def test_a_truncated_file_does_not_raise(tmp_path):
    path = tmp_path / "tiny.iso"
    path.write_bytes(b"MZ")
    table = gpt.parse(path)
    assert not table.has_mbr
    assert not table.has_gpt


def test_a_corrupt_entry_count_does_not_allocate_wildly(iso):
    path = iso()
    data = bytearray(path.read_bytes())
    data[512 + 80 : 512 + 84] = (0xFFFFFFFF).to_bytes(4, "little")
    path.write_bytes(bytes(data))
    assert gpt.parse(path).esp() is None


# ---------------------------------------------------------------------------
# El Torito
# ---------------------------------------------------------------------------


def test_a_bootable_uefi_entry_is_detected():
    assert gpt.parse_el_torito(el_torito_report("UEFI", "y"))


def test_a_non_bootable_entry_is_not():
    assert not gpt.parse_el_torito(el_torito_report("UEFI", "n"))


def test_a_bios_only_image_is_not_mistaken_for_uefi():
    assert not gpt.parse_el_torito(el_torito_report("BIOS", "y"))


def test_the_image_path_alone_does_not_count():
    """`/boot/efi.img` contains 'efi'; the entry must actually be UEFI.

    Matching on the substring would pass an image with no UEFI boot entry at
    all, which is the failure this whole module exists to catch.
    """
    report = el_torito_report("BIOS", "y")
    assert "efi" in report.lower()
    assert not gpt.parse_el_torito(report)
