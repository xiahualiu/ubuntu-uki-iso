"""
Configuration loading and the guard checks.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ubuntu_uki_iso import settings
from ubuntu_uki_iso.rootfs.config import package_list
from ubuntu_uki_iso.settings import Mode
from ubuntu_uki_iso.uki import config as boot

# ---------------------------------------------------------------------------
# Command lines
# ---------------------------------------------------------------------------


def test_the_live_cmdline_has_its_label_substituted() -> None:
    rendered = boot.cmdline(Mode.LIVE)
    assert "@@" not in rendered
    assert f"CDLABEL={settings.VOLID}" in rendered
    assert "rd.live.dir=/payload" in rendered
    assert "rd.live.squashimg=base.squashfs" in rendered


def test_the_live_cmdline_does_not_auto_assemble_the_array() -> None:
    """
    The array stays untouched until the installer deliberately assembles it.
    """
    assert "rd.md=0" in boot.cmdline(Mode.LIVE)


def test_the_installed_cmdline_names_the_gpt_partition_label() -> None:
    rendered = boot.cmdline(Mode.INSTALLED)
    assert f"root=PARTLABEL={settings.ROOT_LABEL}" in rendered
    assert "PARTUUID=" not in rendered
    assert "@@" not in rendered


def test_the_installed_cmdline_derives_only_from_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "ROOT_LABEL", "custom-root")
    assert "root=PARTLABEL=custom-root" in boot.cmdline(Mode.INSTALLED)


# ---------------------------------------------------------------------------
# Everything else that ships
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("variant", [Mode.LIVE, Mode.INSTALLED])
def test_package_lists_load(variant: Mode) -> None:
    packages = package_list(variant)
    assert len(packages) > 10
    assert "python3" in packages, "the installer needs the complete standard library"


@pytest.mark.parametrize("variant", [Mode.LIVE, Mode.INSTALLED])
def test_dracut_configs_render(variant: Mode) -> None:
    assert "hostonly=" in boot.dracut_conf(variant).render()


def test_the_esp_label_fits_in_a_fat_volume_name() -> None:
    """
    mkfs.vfat fails outright at 12 characters, at the last step of the build.
    """
    assert len(settings.ESP_LABEL) <= 11


def test_live_and_target_dracut_policies_have_distinct_boot_requirements() -> None:
    live = boot.dracut_conf(Mode.LIVE)
    server = boot.dracut_conf("uki-build")
    assert "dmsquash-live" in live.modules and "overlayfs" in live.modules
    assert set(server.drivers) == {"nvme", "ahci", "raid0"}
    for variant in (Mode.LIVE, Mode.INSTALLED, "uki-build"):
        rendered = boot.dracut_conf(variant).render()
        assert 'hostonly="no"' in rendered
        assert "omit_" not in rendered
        assert "add_dracutmodules" not in rendered
        assert 'dracutmodules=" ' in rendered
        assert 'drivers=" ' in rendered
    assert boot.dracut_conf(Mode.INSTALLED) == server


def test_dracut_rendering_is_valid_shell_and_replaces_host_lists(tmp_path: Path) -> None:
    import subprocess

    config = boot.dracut_conf(Mode.LIVE)
    path = config.write(tmp_path / "dracut.conf")
    script = 'hostonly="yes"; dracutmodules="unwanted"; drivers="nouveau"; source "$1"; printf "%s\\n" "$hostonly" "$dracutmodules" "$drivers" "$compress"'
    result = subprocess.run(
        ["bash", "-c", script, "bash", str(path)], capture_output=True, text=True, check=True
    )
    assert result.stdout.splitlines() == [
        "no",
        f" {' '.join(config.modules)} ",
        f" {' '.join(config.drivers)} ",
        "zstd",
    ]


def test_python_config_rejects_malformed_names_and_command_lines() -> None:
    from ubuntu_uki_iso.errors import ConfigError

    with pytest.raises(ConfigError):
        boot.DracutConfig(modules=("mdraid; bad",))
    with pytest.raises(ConfigError):
        boot.KernelCommandLine(("root=one two",)).render()


@pytest.mark.parametrize("modules,drivers", [((), ("nvme",)), (("base",), ())])
def test_dracut_rejects_empty_lists(modules: tuple[str, ...], drivers: tuple[str, ...]) -> None:
    from ubuntu_uki_iso.errors import ConfigError

    with pytest.raises(ConfigError, match="must not be empty"):
        boot.DracutConfig(modules=modules, drivers=drivers)


def test_live_console_and_keyboard_support_early_boot_diagnostics() -> None:
    args = boot.cmdline(Mode.LIVE).split()
    assert [arg for arg in args if arg.startswith("console=")] == [
        "console=ttyS0,115200n8",
        "console=tty0",
    ]
    assert {"rd.shell", "rd.debug", "systemd.show_status=1"} <= set(args)
    assert {"usbhid", "hid_generic"} <= set(boot.dracut_conf(Mode.LIVE).drivers)
