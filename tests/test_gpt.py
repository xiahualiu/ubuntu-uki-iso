"""Proving an ISO is actually hybrid.

These checks exist because of a specific silent failure: the flag combination
the design named produces a valid El Torito entry and no partition table at
all. Such an ISO boots from a CD, boots in QEMU with ``-cdrom``, and is a
coaster on a USB stick — with nothing in the build output saying so.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from helpers import BASIC_DATA_TYPE_GUID, ESP_TYPE_GUID, el_torito_report, make_gpt_iso
from ubuntu_uki_iso import gpt


@pytest.fixture
def iso(tmp_path: Path) -> Callable[..., Path]:
    def write(**kwargs: Any) -> Path:
        path = tmp_path / "test.iso"
        path.write_bytes(make_gpt_iso(**kwargs))
        return path

    return write


# ---------------------------------------------------------------------------
# Partition table
# ---------------------------------------------------------------------------


def test_a_correct_hybrid_is_recognised(iso: Callable[..., Path]) -> None:
    table = gpt.parse(iso())
    assert table.has_mbr
    assert table.has_gpt
    esp = table.esp()
    assert esp is not None
    assert esp.name == "Appended2"


def test_the_designs_original_flags_are_caught(iso: Callable[..., Path]) -> None:
    """No MBR and no GPT — which is what `-isohybrid-gpt-basdat` alone gives."""
    table = gpt.parse(iso(mbr=False, gpt=False))
    assert not table.has_mbr
    assert not table.has_gpt
    assert table.esp() is None


def test_an_mbr_without_a_gpt_is_distinguishable(iso: Callable[..., Path]) -> None:
    table = gpt.parse(iso(gpt=False))
    assert table.has_mbr
    assert not table.has_gpt


def test_a_gpt_without_an_esp_type_is_caught(iso: Callable[..., Path]) -> None:
    """The partition is there but firmware will not recognise it as bootable."""
    table = gpt.parse(iso(partitions=[(BASIC_DATA_TYPE_GUID, 64, 2048, "Gap0")]))
    assert table.has_gpt
    assert table.esp() is None


def test_the_esp_is_found_among_other_partitions(iso: Callable[..., Path]) -> None:
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


def test_an_empty_gpt_entry_is_skipped(iso: Callable[..., Path]) -> None:
    table = gpt.parse(iso(partitions=[(bytes(16), 0, 0, "")]))
    assert table.esp() is None


def test_the_partitions_own_guid_is_read_lowercase(iso: Callable[..., Path]) -> None:
    """The installer's check that the disk matches the UKI's command line.

    The on-disk encoding is mixed-endian and the parser formats it uppercase,
    because that is how the type GUIDs are compared. ``root=PARTUUID=`` and
    ``/dev/disk/by-partuuid/`` are lowercase, so anything compared against a
    real PARTUUID has to come through ``partuuid`` rather than ``unique_guid``
    — otherwise the check fails against a disk that is perfectly correct.
    """
    # a0228cdd-e4e1-5447-91d6-305a7b2c0b5a, in the spec's mixed-endian layout
    raw = bytes.fromhex("dd8c22a0e1e447 5491d6305a7b2c0b5a".replace(" ", ""))
    table = gpt.parse(iso(unique_guids=[raw]))

    partition = table.partitions[0]
    assert partition.partuuid == "a0228cdd-e4e1-5447-91d6-305a7b2c0b5a"
    assert partition.unique_guid == partition.partuuid.upper()


def test_partitions_are_addressable_by_number(iso: Callable[..., Path]) -> None:
    """sgdisk numbers them 1 and 2; the installer has to address the same ones."""
    table = gpt.parse(
        iso(
            partitions=[
                (ESP_TYPE_GUID, 64, 2048, "esp"),
                (BASIC_DATA_TYPE_GUID, 2049, 4096, "root"),
            ]
        )
    )

    first, second = table.by_number(1), table.by_number(2)
    assert first is not None and first.name == "esp"
    assert second is not None and second.name == "root"
    assert table.by_number(3) is None


def test_a_truncated_file_does_not_raise(tmp_path: Path) -> None:
    path = tmp_path / "tiny.iso"
    path.write_bytes(b"MZ")
    table = gpt.parse(path)
    assert not table.has_mbr
    assert not table.has_gpt


def test_a_corrupt_entry_count_does_not_allocate_wildly(iso: Callable[..., Path]) -> None:
    path = iso()
    data = bytearray(path.read_bytes())
    data[512 + 80 : 512 + 84] = (0xFFFFFFFF).to_bytes(4, "little")
    path.write_bytes(bytes(data))
    assert gpt.parse(path).esp() is None


# ---------------------------------------------------------------------------
# El Torito
# ---------------------------------------------------------------------------


def test_a_bootable_uefi_entry_is_detected() -> None:
    assert gpt.parse_el_torito(el_torito_report("UEFI", "y"))


def test_a_non_bootable_entry_is_not() -> None:
    assert not gpt.parse_el_torito(el_torito_report("UEFI", "n"))


def test_a_bios_only_image_is_not_mistaken_for_uefi() -> None:
    assert not gpt.parse_el_torito(el_torito_report("BIOS", "y"))


def test_the_image_path_alone_does_not_count() -> None:
    """`/boot/efi.img` contains 'efi'; the entry must actually be UEFI.

    Matching on the substring would pass an image with no UEFI boot entry at
    all, which is the failure this whole module exists to catch.
    """
    report = el_torito_report("BIOS", "y")
    assert "efi" in report.lower()
    assert not gpt.parse_el_torito(report)
