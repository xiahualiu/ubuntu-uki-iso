"""ESP management: retention, and the firmware fallback.

Both concern a 1 GB partition holding ~50 MB files. Retention keeps it from
filling; the fallback keeps the machine bootable without NVRAM. Both run from
kernel-install, on the upgrade path, which is the worst place to discover a bug
in either.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from helpers import make_pe, make_uki
from ubuntu_uki_iso import settings, ukis
from ubuntu_uki_iso.shim import install_kernel_install_plugins, install_kernel_package_hooks
from ubuntu_uki_iso.ukis import fallback, retention, trigger


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


# ---------------------------------------------------------------------------
# The plugin entry points
# ---------------------------------------------------------------------------


def test_the_plugins_ignore_verbs_other_than_add(
    esp: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("KERNEL_INSTALL_BOOT_ROOT", str(esp))
    add_uki(esp, "machine-1.0.efi", 1)

    assert fallback.main(["prog", "remove", "1.0"]) == 0
    assert not (esp / "EFI/BOOT/BOOTX64.EFI").exists()


def test_the_plugins_run_on_add(esp: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KERNEL_INSTALL_BOOT_ROOT", str(esp))
    add_uki(esp, "machine-1.0.efi", 1)

    assert fallback.main(["prog", "add", "1.0"]) == 0
    assert (esp / "EFI/BOOT/BOOTX64.EFI").is_file()


def test_the_plugins_report_a_bad_argv(esp: Path) -> None:
    assert fallback.main(["prog"]) == 1
    assert retention.main(["prog"]) == 1


# ---------------------------------------------------------------------------
# The hook that makes a kernel package install build a UKI
# ---------------------------------------------------------------------------
#
# Nothing in a stock Ubuntu system calls kernel-install. The bindeb-pkg
# maintainer scripts run-parts /etc/kernel/postinst.d and stop there, and
# systemd-boot — which would call it — is deliberately not installed. So these
# hooks are the only thing standing between "the kernel package installed" and
# "the machine has a UKI".


class _Called:
    """Records what kernel-install would have been called with."""

    def __init__(self, returncode: int = 0) -> None:
        self.returncode = returncode
        self.command: list[str] = []
        self.environment: dict[str, str] = {}

    def __call__(self, command: list[str], env: dict[str, str] | None = None) -> int:
        self.command = list(command)
        self.environment = dict(env or {})
        return self.returncode


@pytest.fixture
def kernel_install(monkeypatch: pytest.MonkeyPatch) -> _Called:
    called = _Called()
    monkeypatch.setattr(trigger.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(trigger.subprocess, "call", called)
    return called


def test_the_postinst_hook_builds_a_uki_from_the_kernel_package(
    kernel_install: _Called,
) -> None:
    """run-parts hands the hook the release and the image path, in that order."""
    assert trigger.postinst_main(["prog", "7.0.14-ubuntu-uki-iso", "/boot/vmlinuz-x"]) == 0

    assert kernel_install.command == [
        "/usr/bin/kernel-install",
        "add",
        "7.0.14-ubuntu-uki-iso",
        "/boot/vmlinuz-x",
    ]


def test_the_hook_points_the_boot_root_at_the_esp(kernel_install: _Called) -> None:
    """The whole chain writes the UKI wherever this points.

    kernel-install's own autodetection is a guess on a machine with more than
    one ESP, and it is the guess that decides whether the machine boots — so it
    is set explicitly, and to the same path the fstab mounts.
    """
    trigger.postinst_main(["prog", "1.0", "/boot/vmlinuz-1.0"])

    assert kernel_install.environment["KERNEL_INSTALL_BOOT_ROOT"] == settings.ESP_MOUNT


def test_the_postrm_hook_removes_the_uki(kernel_install: _Called) -> None:
    """A removed kernel's UKI is a boot entry that cannot work."""
    assert trigger.postrm_main(["prog", "1.0"]) == 0

    assert kernel_install.command == ["/usr/bin/kernel-install", "remove", "1.0"]


def test_the_hook_passes_a_failure_through(kernel_install: _Called) -> None:
    """A UKI that will not build is worth failing the install over.

    The alternative is a machine that silently goes on booting the old kernel
    while the operator believes the new one is in place.
    """
    kernel_install.returncode = 1

    assert trigger.postinst_main(["prog", "1.0", "/boot/vmlinuz-1.0"]) == 1


def test_the_hook_does_nothing_without_arguments(kernel_install: _Called) -> None:
    assert trigger.postinst_main(["prog"]) == 0
    assert kernel_install.command == []


def test_the_hooks_are_installed_and_executable(tmp_path: Path) -> None:
    written = install_kernel_package_hooks(tmp_path)

    relative = sorted(path.relative_to(tmp_path).as_posix() for path in written)
    assert relative == [
        "etc/kernel/postinst.d/zz-ubuntu-uki-iso",
        "etc/kernel/postrm.d/zz-ubuntu-uki-iso",
    ]

    postinst = tmp_path / "etc/kernel/postinst.d/zz-ubuntu-uki-iso"
    assert os.access(postinst, os.X_OK)
    assert "sys.exit(postinst_main())" in postinst.read_text()


def test_the_install_plugins_and_the_package_hooks_do_not_collide(tmp_path: Path) -> None:
    """Two kinds of hook, three directories, no shared file names."""
    plugins = {path.name for path in install_kernel_install_plugins(tmp_path)}
    hooks = {path.name for path in install_kernel_package_hooks(tmp_path)}

    assert plugins.isdisjoint(hooks)
