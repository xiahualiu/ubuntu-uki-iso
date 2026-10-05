"""Tests for kernel config overrides and guard entries."""

from __future__ import annotations

import pytest

from ubuntu_uki_iso.errors import ConfigError
from ubuntu_uki_iso.kernel import config as kconfig
from ubuntu_uki_iso.kernel.tests import validation
from ubuntu_uki_iso.kernel.tests.guard_entry import GuardEntry


def test_the_packaged_guard_list_parses() -> None:
    entries = validation.guard_entries()
    assert len(entries) > 20
    assert all(symbol.startswith("CONFIG_") for symbol, _ in entries)


def test_nothing_on_the_guard_list_is_trimmed() -> None:
    assert validation.guard_overlap() == []


def test_every_guard_symbol_is_a_real_requirement() -> None:
    for _, requirement in validation.guard_entries():
        assert requirement in (None, "y", "m")


@pytest.mark.parametrize(
    ("requirement", "value", "expected"),
    [
        (None, "y", True),
        (None, "m", True),
        (None, None, False),
        ("y", "y", True),
        ("y", "m", False),
        ("y", None, False),
        ("m", "y", True),
        ("m", "m", True),
        ("m", None, False),
    ],
)
def test_guard_requirements(requirement: str | None, value: str | None, expected: bool) -> None:
    assert GuardEntry("CONFIG_FOO", requirement).satisfied_by(value) is expected


def test_check_guard_reports_what_is_missing() -> None:
    failures = validation.check_guard("")
    assert len(failures) == len(validation.guard_entries())
    assert any("CONFIG_OVERLAY_FS" in failure for failure in failures)


def test_check_guard_reports_a_disabled_symbol() -> None:
    failures = validation.check_guard("# CONFIG_OVERLAY_FS is not set\n")
    assert any("CONFIG_OVERLAY_FS" in failure for failure in failures)


def test_check_guard_is_silent_when_everything_is_present() -> None:
    text = "\n".join(
        f"{symbol}={requirement or 'y'}" for symbol, requirement in validation.guard_entries()
    )
    assert validation.check_guard(text) == []


def test_parse_config_values_distinguishes_unset_from_absent() -> None:
    values = validation.parse_config_values("CONFIG_A=y\n# CONFIG_B is not set\n")
    assert values["CONFIG_A"] == "y"
    assert values["CONFIG_B"] is None
    assert "CONFIG_C" not in values


def test_a_typo_in_the_config_override_is_detected() -> None:
    unknown = validation.unknown_config_override_symbols(
        "CONFIG_REAL=y\n", kconfig.config_override()
    )
    assert "CONFIG_REAL" not in unknown
    assert unknown


def test_kernel_override_is_valid() -> None:
    validation.validate_config_override(kconfig.config_override())
    assert "CONFIG_LOCALVERSION=" in kconfig.render_config_override()
    assert validation.guard_entries()


def test_the_guard_covers_what_the_live_medium_arrives_on() -> None:
    guarded = {symbol for symbol, _ in validation.guard_entries()}
    required = {
        "CONFIG_USB_STORAGE",
        "CONFIG_USB_XHCI_HCD",
        "CONFIG_SCSI",
        "CONFIG_BLK_DEV_SD",
        "CONFIG_ISO9660_FS",
        "CONFIG_SQUASHFS",
        "CONFIG_SATA_AHCI",
        "CONFIG_BLK_DEV_NVME",
    }
    assert required <= guarded


def test_the_guard_does_not_still_guard_the_virtio_stack() -> None:
    guarded = {symbol for symbol, _ in validation.guard_entries()}
    assert not {"CONFIG_SCSI_VIRTIO", "CONFIG_VIRTIO_BLK"} & guarded


def test_the_guard_covers_the_storage_this_machine_uses() -> None:
    guarded = {symbol for symbol, _ in validation.guard_entries()}
    assert {"CONFIG_SATA_AHCI", "CONFIG_BLK_DEV_NVME", "CONFIG_MD_RAID0"} <= guarded


def test_generated_kernel_config_overrides_are_written_outside_the_package(tmp_path) -> None:
    path = tmp_path / "config.override"
    path.write_text(kconfig.render_config_override(), encoding="utf-8")
    values = validation.parse_config_values(path.read_text())
    assert values["CONFIG_LOCALVERSION"] == "-xiahualab-iso"
    assert values["CONFIG_WLAN"] is None


def test_kernel_config_rejects_invalid_guard_and_duplicate_config_override_symbols() -> None:
    with pytest.raises(ConfigError):
        GuardEntry("CONFIG_NVME", "n")
    with pytest.raises(ConfigError):
        validation.validate_config_override(
            [
                kconfig.ConfigEntry("CONFIG_X", kconfig.TristateValue.Y),
                kconfig.ConfigEntry("CONFIG_X", kconfig.TristateValue.N),
            ]
        )


@pytest.mark.parametrize("value", list(kconfig.TristateValue))
def test_config_entry_renders_tristate_values(value) -> None:
    entry = kconfig.ConfigEntry("CONFIG_TEST", value)
    expected = (
        "# CONFIG_TEST is not set\n"
        if value is kconfig.TristateValue.N
        else f"CONFIG_TEST={value}\n"
    )
    assert entry.render() == expected


def test_workstation_requirements_are_not_explicitly_disabled() -> None:
    disabled = validation.disabled_symbols(kconfig.config_override())
    for group, symbols in validation.workstation_requirements().items():
        assert not (set(symbols) & disabled), group
