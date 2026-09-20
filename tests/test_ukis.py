"""ESP management: retention, and the firmware fallback.

Both concern a 1 GB partition holding ~50 MB files. Retention keeps it from
filling; the fallback keeps the machine bootable without NVRAM. Both are called
by the UKI package's postinst, which runs at every install and every kernel
upgrade — the worst place to discover a bug in either.

The postinst itself, including the order it calls these in, is covered by
``test_ukis_postinst.py``.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from helpers import make_pe, make_uki
from ubuntu_uki_iso import settings, ukis
from ubuntu_uki_iso.ukis import fallback, retention


@pytest.fixture
def esp(tmp_path: Path) -> Path:
    """A boot root with an empty EFI tree."""
    (tmp_path / "EFI/Linux").mkdir(parents=True)
    (tmp_path / "EFI/BOOT").mkdir(parents=True)
    return tmp_path


def add_uki(esp: Path, name: str, age_days: float) -> Path:
    path = esp / "EFI/Linux" / name
    path.write_bytes(make_uki())
    when = path.stat().st_mtime - age_days * 86400
    os.utime(path, (when, when))
    return path


# ---------------------------------------------------------------------------
# Retention
# ---------------------------------------------------------------------------


def test_nothing_is_removed_below_the_limit(esp: Path) -> None:
    add_uki(esp, "a-1.0.efi", 3)
    add_uki(esp, "a-2.0.efi", 2)
    assert retention.prune(esp, keep=3, running_release="2.0") == []


def test_the_oldest_are_removed_first(esp: Path) -> None:
    """`keep` counts UKIs *in addition to* the protected ones.

    A system that keeps two and has a protected running kernel ends up with
    three files. That is intentional: the alternative is that protecting a
    kernel silently reduces retention, which is the kind of arithmetic that
    eats the rollback you were counting on.
    """
    for index in range(1, 6):
        add_uki(esp, f"machine-{index}.0.efi", 6 - index)

    removed = retention.prune(esp, keep=2, running_release="5.0", just_installed="5.0")

    assert {path.name for path in removed} == {"machine-1.0.efi", "machine-2.0.efi"}
    assert {path.name for path in (esp / "EFI/Linux").iterdir()} == {
        "machine-3.0.efi",
        "machine-4.0.efi",
        "machine-5.0.efi",  # protected, despite being newest
    }


def test_the_running_kernel_survives_even_when_it_is_the_oldest(esp: Path) -> None:
    """Removing the executing kernel's UKI is the kind of thing that works
    fine until the next reboot."""
    add_uki(esp, "oldest-running.efi", 100)
    for index in range(1, 5):
        add_uki(esp, f"newer-{index}.efi", 5 - index)

    retention.prune(esp, keep=1, running_release="oldest-running", just_installed="newer-4")

    assert (esp / "EFI/Linux/oldest-running.efi").is_file()


def test_the_just_installed_kernel_survives(esp: Path) -> None:
    """The subtle one.

    This runs from kernel-install, so the running kernel is still the *old*
    one and the new UKI is the one that must not be pruned. Sorting by age
    alone would get this right only by accident.
    """
    for index in range(1, 5):
        add_uki(esp, f"machine-{index}.0.efi", 5 - index)

    retention.prune(esp, keep=1, running_release="1.0", just_installed="4.0")

    remaining = {path.name for path in (esp / "EFI/Linux").iterdir()}
    assert "machine-4.0.efi" in remaining
    assert "machine-1.0.efi" in remaining


def test_keeping_zero_is_refused(esp: Path) -> None:
    add_uki(esp, "a.efi", 1)
    add_uki(esp, "b.efi", 2)
    assert retention.prune(esp, keep=0, running_release="a") == []
    assert len(list((esp / "EFI/Linux").iterdir())) == 2


def test_a_missing_directory_is_not_an_error(tmp_path: Path) -> None:
    assert retention.prune(tmp_path / "nothing-here", keep=3, running_release="x") == []


def test_find_ukis_returns_newest_first(esp: Path) -> None:
    add_uki(esp, "old.efi", 10)
    add_uki(esp, "new.efi", 1)
    assert [uki.name for uki in retention.find_ukis(esp)] == ["new.efi", "old.efi"]


def test_retention_ignores_non_efi_files(esp: Path) -> None:
    add_uki(esp, "real.efi", 1)
    (esp / "EFI/Linux/notes.txt").write_text("not a uki")
    retention.prune(esp, keep=0, running_release="real", just_installed="real")
    assert (esp / "EFI/Linux/real.efi").is_file()
    assert (esp / "EFI/Linux/notes.txt").is_file()


# ---------------------------------------------------------------------------
# The firmware fallback
# ---------------------------------------------------------------------------


def test_the_uki_is_copied_to_the_firmware_path(esp: Path) -> None:
    add_uki(esp, "machine-1.0.efi", 1)

    installed = fallback.install(esp, "1.0")
    assert installed is not None

    assert installed == esp / "EFI/BOOT/BOOTX64.EFI"
    assert installed.is_file()
    assert installed.read_bytes() == (esp / "EFI/Linux/machine-1.0.efi").read_bytes()


def test_a_foreign_loader_is_preserved_once(esp: Path) -> None:
    """shim and GRUB are worth keeping; our own previous copy is not."""
    shim = esp / "EFI/BOOT/BOOTX64.EFI"
    shim.write_bytes(make_pe({".text": b"\x90" * 256}))  # no .cmdline section
    add_uki(esp, "machine-1.0.efi", 1)

    fallback.install(esp, "1.0")

    backup = esp / "EFI/BOOT/BOOTX64.EFI.pre-ubuntu-uki-iso"
    assert backup.is_file()
    assert b"\x90" in backup.read_bytes()


def test_the_backup_is_never_overwritten(esp: Path) -> None:
    """A second run that clobbered it would eventually destroy the only copy
    of the loader known to work."""
    shim = esp / "EFI/BOOT/BOOTX64.EFI"
    shim.write_bytes(make_pe({".text": b"\x90" * 256}))
    add_uki(esp, "machine-1.0.efi", 1)

    fallback.install(esp, "1.0")
    backup = esp / "EFI/BOOT/BOOTX64.EFI.pre-ubuntu-uki-iso"
    original_backup = backup.read_bytes()

    add_uki(esp, "machine-2.0.efi", 0)
    fallback.install(esp, "2.0")

    assert backup.read_bytes() == original_backup


def test_our_own_previous_uki_is_not_backed_up(esp: Path) -> None:
    """Otherwise every upgrade would quietly cost another 50 MB of ESP."""
    add_uki(esp, "machine-1.0.efi", 2)
    fallback.install(esp, "1.0")

    add_uki(esp, "machine-2.0.efi", 1)
    fallback.install(esp, "2.0")

    assert not (esp / "EFI/BOOT/BOOTX64.EFI.pre-ubuntu-uki-iso").exists()


def test_a_missing_uki_leaves_the_fallback_alone(esp: Path) -> None:
    add_uki(esp, "machine-1.0.efi", 1)
    fallback.install(esp, "1.0")
    before = (esp / "EFI/BOOT/BOOTX64.EFI").read_bytes()

    assert fallback.install(esp, "9.9") is None
    assert (esp / "EFI/BOOT/BOOTX64.EFI").read_bytes() == before


def test_the_fallback_path_matches_the_constant() -> None:
    assert ukis.FALLBACK_RELPATH.as_posix() == "EFI/BOOT/BOOTX64.EFI"
    assert settings.FALLBACK_EFI_PATH == r"\EFI\BOOT\BOOTX64.EFI"


def test_looks_like_uki_distinguishes_a_uki_from_a_loader(tmp_path: Path) -> None:
    uki = tmp_path / "a.efi"
    uki.write_bytes(make_uki())
    loader = tmp_path / "b.efi"
    loader.write_bytes(make_pe({".text": b"\x90" * 64}))

    assert fallback.looks_like_uki(uki)
    assert not fallback.looks_like_uki(loader)
    assert not fallback.looks_like_uki(tmp_path / "missing.efi")
