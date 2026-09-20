"""The one thing that puts a UKI on the target's ESP.

This runs on the target, from dpkg, at install time and at every kernel
upgrade — which is the property the whole design rests on. So the tests here
are about the two ways it could quietly leave a machine unbootable: writing to
a directory that is not the ESP, or placing a UKI for a kernel whose modules
are not on the machine.
"""

from __future__ import annotations

import io
from pathlib import Path

import pytest

from ubuntu_uki_iso import settings
from ubuntu_uki_iso.errors import ConfigError
from ubuntu_uki_iso.log import Console
from ubuntu_uki_iso.ukis import postinst

RELEASE = "7.0.14-ubuntu-uki-iso"
UKI_BYTES = b"MZ" + b"\0" * 4094
ENTRY = f"{settings.ENTRY_TOKEN}-{RELEASE}.efi"


@pytest.fixture
def package(tmp_path: Path) -> Path:
    """A package payload, laid out the way the UKI package is."""
    payload = tmp_path / "package"
    (payload / "lib/modules" / RELEASE).mkdir(parents=True)
    (payload / "uki.efi").write_bytes(UKI_BYTES)
    (payload / "release").write_text(f"{RELEASE}\n", encoding="utf-8")
    return payload


@pytest.fixture
def wired(monkeypatch: pytest.MonkeyPatch, package: Path, tmp_path: Path) -> Path:
    """Point the module at the fake payload and a boot root that exists."""
    boot_root = tmp_path / "esp"
    boot_root.mkdir()

    monkeypatch.setattr(postinst, "PACKAGED_UKI", package / "uki.efi")
    monkeypatch.setattr(postinst, "RELEASE_FILE", package / "release")
    monkeypatch.setattr(postinst, "MODULES_DIR", package / "lib/modules")
    # tmp_path is not a mount point, and the check that it is one is deliberate
    # — so it is stubbed rather than removed.
    monkeypatch.setattr(postinst.os.path, "ismount", lambda path: True)
    return boot_root


def _console() -> Console:
    return Console(colour=False, stdout=io.StringIO(), stderr=io.StringIO())


def test_the_uki_lands_under_efi_linux_and_as_the_fallback(wired: Path) -> None:
    """Both paths, and they are the same bytes.

    ``EFI/Linux`` is where retention prunes and ``--bless`` looks; the fallback
    is the only path firmware finds with no configuration, and the only one
    that survives losing NVRAM.
    """
    installed = postinst.install(wired, console=_console())

    assert installed == wired / "EFI/Linux" / ENTRY
    assert installed.read_bytes() == UKI_BYTES
    assert (wired / "EFI/BOOT/BOOTX64.EFI").read_bytes() == UKI_BYTES


def test_an_esp_that_is_not_a_mount_point_is_refused(
    wired: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The failure this prevents reports success.

    A UKI written into an ordinary directory on the root filesystem is one the
    firmware will never look at, and nothing about the install would say so.
    """
    monkeypatch.setattr(postinst.os.path, "ismount", lambda path: False)

    with pytest.raises(ConfigError, match="not mounted"):
        postinst.install(wired, console=_console())


def test_a_package_whose_modules_are_absent_is_refused(wired: Path, package: Path) -> None:
    """A UKI for a kernel with no modules is a machine that boots and cannot
    load a driver, which is worse than one that does not boot."""
    (package / "lib/modules" / RELEASE).rmdir()

    with pytest.raises(ConfigError, match="is not there"):
        postinst.install(wired, console=_console())


def test_a_package_with_no_release_file_is_refused(wired: Path, package: Path) -> None:
    (package / "release").unlink()

    with pytest.raises(ConfigError, match="no release file"):
        postinst.install(wired, console=_console())


def test_an_empty_release_file_is_refused(wired: Path, package: Path) -> None:
    (package / "release").write_text("", encoding="utf-8")

    with pytest.raises(ConfigError, match="empty"):
        postinst.install(wired, console=_console())


def test_pruning_keeps_the_uki_that_was_just_installed(
    wired: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The subtle one, and the reason retention has a ``just_installed``.

    At this moment the running kernel is the *old* one, so sorting by age alone
    would be right only by accident.
    """
    efi_linux = wired / "EFI/Linux"
    efi_linux.mkdir(parents=True)
    for name in ("old-a", "old-b", "old-c", "old-d"):
        (efi_linux / f"{name}.efi").write_bytes(b"old")
    monkeypatch.setattr(settings, "UKI_RETENTION", 1)

    postinst.install(wired, console=_console())

    assert (efi_linux / ENTRY).is_file(), "the UKI just placed must survive its own prune"


def test_other_dpkg_verbs_leave_the_esp_alone(wired: Path) -> None:
    """``abort-upgrade`` and friends mean the package is *not* being installed."""
    for verb in ("abort-upgrade", "abort-remove", "disappear", "triggered"):
        assert postinst.main(["postinst", verb]) == 0
    assert not (wired / "EFI").exists()
