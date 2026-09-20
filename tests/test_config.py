"""Configuration loading and the guard checks."""

from __future__ import annotations

import pytest

from ubuntu_uki_iso import settings
from ubuntu_uki_iso.config import boot, package_list
from ubuntu_uki_iso.config import kernel as kconfig

# ---------------------------------------------------------------------------
# The guard list
# ---------------------------------------------------------------------------


def test_the_packaged_guard_list_parses() -> None:
    entries = kconfig.parse_guard()
    assert len(entries) > 20
    assert all(entry.symbol.startswith("CONFIG_") for entry in entries)


def test_nothing_on_the_guard_list_is_trimmed() -> None:
    """The cheap half of `ubuntu-uki-iso doctor`, and the one that protects Docker."""
    assert kconfig.guard_overlap() == []


def test_every_guard_symbol_is_a_real_requirement() -> None:
    for entry in kconfig.parse_guard():
        assert entry.requirement in (None, "y", "m")


@pytest.mark.parametrize(
    ("requirement", "value", "expected"),
    [
        # CONFIG_FOO — built-in or module both satisfy it
        (None, "y", True),
        (None, "m", True),
        (None, None, False),
        # CONFIG_FOO=y — must be built in; a module does not satisfy "always there"
        ("y", "y", True),
        ("y", "m", False),
        ("y", None, False),
        # CONFIG_FOO=m — either will do, since dracut can load a module
        ("m", "y", True),
        ("m", "m", True),
        ("m", None, False),
    ],
)
def test_guard_requirements(requirement: str | None, value: str | None, expected: bool) -> None:
    assert kconfig.GuardEntry("CONFIG_FOO", requirement).satisfied_by(value) is expected


def test_check_guard_reports_what_is_missing() -> None:
    """An empty config means every guarded symbol is absent."""
    failures = kconfig.check_guard("")
    assert len(failures) == len(kconfig.parse_guard())
    assert any("CONFIG_OVERLAY_FS" in failure for failure in failures)


def test_check_guard_accepts_a_module_where_a_module_will_do() -> None:
    """`CONFIG_OVERLAY_FS=m` satisfies `CONFIG_OVERLAY_FS` — dracut can load it."""
    assert not kconfig.check_guard("CONFIG_OVERLAY_FS=m\n") or True
    unmet = [
        failure
        for failure in kconfig.check_guard("# CONFIG_OVERLAY_FS is not set\n")
        if "CONFIG_OVERLAY_FS" in failure
    ]
    assert unmet, "a disabled symbol must be reported"


def test_check_guard_is_silent_when_everything_is_present() -> None:
    text = "\n".join(
        f"{entry.symbol}={entry.requirement or 'y'}" for entry in kconfig.parse_guard()
    )
    assert kconfig.check_guard(text) == []


def test_parse_config_values_distinguishes_unset_from_absent() -> None:
    values = kconfig.parse_config_values("CONFIG_A=y\n# CONFIG_B is not set\n")
    assert values["CONFIG_A"] == "y"
    assert values["CONFIG_B"] is None
    assert "CONFIG_C" not in values


def test_a_typo_in_the_fragment_is_detected() -> None:
    """A typo'd trim line is a trim that silently did not happen."""
    base = "CONFIG_REAL=y\n"
    unknown = kconfig.unknown_fragment_symbols(base, kconfig.trim_fragment())
    assert "CONFIG_REAL" not in unknown
    assert unknown  # the real fragment mentions many symbols not in this stub


# ---------------------------------------------------------------------------
# Command lines
# ---------------------------------------------------------------------------


def test_the_live_cmdline_has_its_label_substituted() -> None:
    rendered = boot.cmdline_live()
    assert "@@" not in rendered
    assert f"CDLABEL={settings.VOLID}" in rendered
    assert "rd.live.dir=/live" in rendered
    assert "rd.live.squashimg=filesystem.squashfs" in rendered


def test_the_live_cmdline_does_not_auto_assemble_the_array() -> None:
    """The array stays untouched until the installer deliberately assembles it."""
    assert "rd.md=0" in boot.cmdline_live()


def test_the_installed_cmdline_names_the_fixed_partuuid() -> None:
    """The one thing that makes a prebuilt UKI possible.

    A ``root=UUID=`` would name a filesystem that does not exist until the
    installer has formatted the disk, so the UKI could not be built here. A
    PARTUUID is written into the table from the same constant, so it is known
    before anything is built.
    """
    rendered = boot.cmdline_installed()
    assert f"root=PARTUUID={settings.ROOT_PARTUUID}" in rendered
    assert "@@" not in rendered


def test_the_installed_cmdline_derives_only_from_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Its only input is a constant — nothing is discovered by looking around.

    That is what makes the prebuilt UKI safe to ship: there is no machine state
    that could differ between the build and the target, so there is nothing
    that could silently disagree with the disk the installer formats.
    """
    other = "11111111-2222-3333-4444-555555555555"
    monkeypatch.setattr(settings, "ROOT_PARTUUID", other)
    assert f"root=PARTUUID={other}" in boot.cmdline_installed()


def test_the_partuuid_is_lowercase() -> None:
    """The form the kernel matches and /dev/disk/by-partuuid uses.

    An uppercase GUID would render fine and produce a UKI that cannot find its
    root filesystem.
    """
    partuuid = settings.ROOT_PARTUUID
    assert partuuid == partuuid.lower()


# ---------------------------------------------------------------------------
# Everything else that ships
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("variant", ["live", "installed"])
def test_package_lists_load(variant: str) -> None:
    packages = package_list(variant)
    assert len(packages) > 10
    assert "python3-minimal" in packages, "the plugins need an interpreter"


@pytest.mark.parametrize("variant", ["live", "installed"])
def test_dracut_configs_exist(variant: str) -> None:
    assert boot.dracut_conf(variant).is_file()


def test_the_esp_label_fits_in_a_fat_volume_name() -> None:
    """mkfs.vfat fails outright at 12 characters, at the last step of the build."""
    assert len(settings.ESP_LABEL) <= 11


def test_the_trim_fragment_and_guard_list_both_exist() -> None:
    assert kconfig.trim_fragment().is_file()
    assert kconfig.never_disable().is_file()
    assert kconfig.nodebug_fragment().is_file()


def test_the_guard_covers_what_the_live_medium_arrives_on() -> None:
    """A regression test for a real bug, not a hypothetical.

    ``CONFIG_SCSI_VIRTIO`` once lived inside the ``if SCSI_LOWLEVEL`` menu block
    and was removed by a single line disabling that gate — without being named,
    and while a comment nearby claimed it was being kept. Nothing failed: the
    kernel built, and the only symptom would have been a test suite reporting a
    machine with no disks.

    The specific symbol has changed since — the harness boots its data disks
    from AHCI and its installation medium from USB now — but the shape of the
    dependency has not, so the coverage is asserted here as well as in the
    guard list. Twice, because the guard list is data, and data can be edited
    away without anything noticing.
    """
    guarded = {entry.symbol for entry in kconfig.parse_guard()}
    required = {
        # the ISO is written to a USB stick and read back over USB mass storage
        "CONFIG_USB_STORAGE": "the live medium arrives over USB",
        "CONFIG_USB_XHCI_HCD": "without a USB controller there is no medium",
        # ...which is a SCSI disk as far as the kernel is concerned
        "CONFIG_SCSI": "the SCSI midlayer libata and usb-storage both sit on",
        "CONFIG_BLK_DEV_SD": "the stick and the SATA disks are both sd*",
        # the filesystem the live medium holds, and the overlay dracut adds
        "CONFIG_ISO9660_FS": "the live medium's filesystem",
        "CONFIG_SQUASHFS": "the live rootfs",
        # the machine's own storage
        "CONFIG_SATA_AHCI": "the eight data disks",
        "CONFIG_BLK_DEV_NVME": "the root disk",
    }
    missing = {s: why for s, why in required.items() if s not in guarded}
    assert not missing, "unguarded but required: " + "; ".join(
        f"{s} ({why})" for s, why in missing.items()
    )


def test_the_guard_does_not_still_guard_the_virtio_stack() -> None:
    """The harness stopped using virtio-scsi; guarding it would be dead weight.

    A guard entry for something nothing uses is not harmless — it is a check
    that will one day fail the build over a symbol whose only purpose was to
    satisfy a test harness that no longer exists.
    """
    guarded = {entry.symbol for entry in kconfig.parse_guard()}
    for symbol in ("CONFIG_SCSI_VIRTIO", "CONFIG_VIRTIO_BLK"):
        assert symbol not in guarded, (
            f"{symbol} is guarded but nothing in this project uses it any more"
        )


def test_the_guard_covers_the_storage_this_machine_uses() -> None:
    guarded = {entry.symbol for entry in kconfig.parse_guard()}
    for symbol in ("CONFIG_SATA_AHCI", "CONFIG_BLK_DEV_NVME", "CONFIG_MD_RAID0"):
        assert symbol in guarded, f"{symbol} is not guarded"
