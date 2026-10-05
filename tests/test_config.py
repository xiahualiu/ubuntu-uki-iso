"""Configuration loading and the guard checks."""

from __future__ import annotations

import pytest

from ubuntu_uki_iso import settings
from ubuntu_uki_iso.config import package_list
from ubuntu_uki_iso.uki import config as boot

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
def test_dracut_configs_render(variant: str) -> None:
    assert "hostonly=" in boot.dracut_conf(variant).render()


def test_the_esp_label_fits_in_a_fat_volume_name() -> None:
    """mkfs.vfat fails outright at 12 characters, at the last step of the build."""
    assert len(settings.ESP_LABEL) <= 11


def test_live_and_target_dracut_policies_have_distinct_boot_requirements() -> None:
    live = boot.dracut_conf("live")
    server = boot.dracut_conf("uki-build")
    assert not live.hostonly and not server.hostonly
    assert "dmsquash-live" in live.modules and "overlayfs" in live.modules
    assert set(server.drivers) == {"nvme", "ahci", "raid0"}
    assert "nouveau" in live.omit_drivers and "nouveau" in server.omit_drivers
    assert boot.dracut_conf("installed").hostonly


def test_dracut_rendering_is_valid_shell_and_preserves_appended_values(tmp_path) -> None:
    import subprocess

    path = boot.dracut_conf("live").write(tmp_path / "dracut.conf")
    script = 'add_dracutmodules="existing "; source "$1"; printf "%s\\n" "$hostonly" "$add_dracutmodules" "$omit_drivers" "$compress"'
    result = subprocess.run(
        ["bash", "-c", script, "bash", str(path)], capture_output=True, text=True, check=True
    )
    assert result.stdout.splitlines() == [
        "no",
        "existing  dmsquash-live overlayfs mdraid ",
        " nouveau ",
        "zstd",
    ]


def test_python_config_rejects_malformed_names_and_command_lines() -> None:
    from ubuntu_uki_iso.errors import ConfigError

    with pytest.raises(ConfigError):
        boot.DracutConfig(modules=("mdraid; bad",))
    with pytest.raises(ConfigError):
        boot.KernelCommandLine(("root=one two",)).render()
