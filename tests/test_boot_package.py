"""
Exercise real Debian packaging and target-only package installation.
"""

import os
import subprocess
from pathlib import Path

import pytest

from helpers import make_pe
from ubuntu_uki_iso.boot_package import EFI_PATH, extract_package
from ubuntu_uki_iso.errors import BuildError
from ubuntu_uki_iso.settings import Mode
from ubuntu_uki_iso.uki.config import cmdline
from ubuntu_uki_iso.uki.package import create_package


@pytest.mark.parametrize("mode", [Mode.LIVE, Mode.INSTALLED])
def test_real_package_contents_and_validation(tmp_path: Path, mode: Mode) -> None:
    modules = tmp_path / "7.0-test"
    modules.mkdir()
    (modules / "test.ko").write_bytes(b"module")
    (modules / "build").symlink_to("/usr/src/headers")
    uki = tmp_path / "uki.efi"
    uki.write_bytes(
        make_pe(
            {
                ".linux": b"kernel",
                ".initrd": b"initrd",
                ".uname": b"7.0-test",
                ".cmdline": cmdline(mode).encode(),
            }
        )
    )
    package = create_package(uki, modules, "7.0-test", mode, tmp_path / "boot.deb")
    assert extract_package(package, tmp_path / "extracted", mode) == "7.0-test"
    control = tmp_path / "control"
    subprocess.run(["dpkg-deb", "--control", package, control], check=True)
    assert (control / "postinst").exists() == (mode == Mode.INSTALLED)
    if mode == Mode.INSTALLED:
        assert (control / "postinst").stat().st_mode & 0o111
        assert "efibootmgr" in (control / "control").read_text()
    assert not (tmp_path / "extracted/usr/lib/modules/7.0-test/build").is_symlink()
    with pytest.raises(BuildError, match="command line"):
        extract_package(
            package, tmp_path / "wrong-mode", Mode.INSTALLED if mode == Mode.LIVE else Mode.LIVE
        )


@pytest.mark.skipif(os.geteuid() != 0, reason="dpkg ownership preservation requires root")
@pytest.mark.parametrize("mode", [Mode.LIVE, Mode.INSTALLED])
def test_real_package_unpacks_into_target_database(tmp_path: Path, mode: Mode) -> None:
    test_real_package_contents_and_validation(tmp_path, mode)
    package = tmp_path / "boot.deb"
    uki = tmp_path / "uki.efi"
    root = tmp_path / "target"
    database = root / "var/lib/dpkg"
    database.mkdir(parents=True)
    # Represent the dependency mmdebstrap installs before the boot package.
    (database / "status").write_text(
        "Package: kmod\nStatus: install ok installed\nArchitecture: amd64\nVersion: 1\n"
        "Maintainer: Test <test@localhost>\nDescription: test dependency\n\n"
    )
    subprocess.run(
        ["dpkg", f"--root={root}", f"--log={tmp_path}/dpkg.log", "--unpack", package],
        check=True,
    )
    assert (root / EFI_PATH).read_bytes() == uki.read_bytes()
    assert (root / "usr/lib/modules/7.0-test/test.ko").read_bytes() == b"module"
    assert f"Package: ubuntu-uki-boot-{mode}" in (database / "status").read_text()
    assert (database / f"info/ubuntu-uki-boot-{mode}.list").is_file()


def test_failed_packaging_preserves_existing_package(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ubuntu_uki_iso.uki import package as boot_package

    modules = tmp_path / "7.0-test"
    modules.mkdir()
    uki = tmp_path / "uki.efi"
    uki.write_bytes(
        make_pe(
            {
                ".linux": b"kernel",
                ".initrd": b"initrd",
                ".uname": b"7.0-test",
                ".cmdline": cmdline(Mode.INSTALLED).encode(),
            }
        )
    )
    output = tmp_path / "boot.deb"
    output.write_bytes(b"previous")

    def fail(*args: object, **kwargs: object) -> None:
        raise subprocess.CalledProcessError(1, ["dpkg-deb"])

    monkeypatch.setattr(boot_package.subprocess, "run", fail)
    with pytest.raises(subprocess.CalledProcessError):
        create_package(uki, modules, "7.0-test", Mode.INSTALLED, output)
    assert output.read_bytes() == b"previous"
    assert not list(tmp_path.glob("boot-deb-*"))


@pytest.mark.parametrize("existing", [False, True])
@pytest.mark.parametrize("device_path", ["", "\tHD(1,GPT,test)/\\EFI\\BOOT\\BOOTX64.EFI"])
def test_postinst_registration_is_opt_in_and_repeatable(
    tmp_path: Path, device_path: str, existing: bool
) -> None:
    from ubuntu_uki_iso.uki.postinst import render

    tools = tmp_path / "bin"
    tools.mkdir()
    script = tmp_path / "postinst"
    script.write_text(render())
    script.chmod(0o755)
    subprocess.run(["sh", "-n", script], check=True)
    state = tmp_path / "entries"
    original = (
        "BootOrder: 0001,0003\nBootNext: 0003\nBoot0001* other-system\nBoot0003* xiahualab-backup\n"
    )
    if existing:
        original += f"Boot0002* xiahualab{device_path}\n"
    state.write_text(original)
    log = tmp_path / "commands"
    efibootmgr = tools / "efibootmgr"
    efibootmgr.write_text("""#!/bin/sh
set -eu
printf '%s\\n' "$*" >> "$EFI_TEST_LOG"
case "${1:-}" in
  '') cat "$EFI_TEST_STATE" ;;
  --bootnum)
    grep -v "^Boot$2" "$EFI_TEST_STATE" > "$EFI_TEST_STATE.tmp"
    mv "$EFI_TEST_STATE.tmp" "$EFI_TEST_STATE" ;;
  --create-only) printf 'Boot000A* xiahualab%s\\n' "$EFI_TEST_DEVICE_PATH" >> "$EFI_TEST_STATE" ;;
esac
""")
    efibootmgr.chmod(0o755)
    mountpoint = tools / "mountpoint"
    mountpoint.write_text('#!/bin/sh\nexit "${EFI_TEST_MOUNT_FAILURE:-0}"\n')
    mountpoint.chmod(0o755)
    environment = dict(os.environ)
    environment.update(
        {
            "PATH": str(tools) + os.pathsep + os.defpath,
            "EFI_TEST_LOG": str(log),
            "EFI_TEST_DEVICE_PATH": device_path,
            "EFI_TEST_STATE": str(state),
            "UBUNTU_UKI_REGISTER_EFI": "0",
        }
    )
    subprocess.run([script, "configure"], check=True, env=environment)
    assert not log.exists()
    environment.update(
        {
            "UBUNTU_UKI_REGISTER_EFI": "1",
            "UBUNTU_UKI_EFI_DISK": "/dev/test",
            "UBUNTU_UKI_EFI_PART": "1",
        }
    )
    subprocess.run([script, "configure"], check=True, env=environment)
    subprocess.run([script, "configure"], check=True, env=environment)
    entries = state.read_text()
    assert entries.count(f"xiahualab{device_path}\n") == 1
    assert "other-system" in entries and "xiahualab-backup" in entries
    commands = log.read_text().splitlines()
    writes = [command for command in commands if command]
    assert writes == (
        []
        if existing
        else [
            "--create-only --disk /dev/test --part 1 --label xiahualab --loader \\EFI\\BOOT\\BOOTX64.EFI"
        ]
    )
    assert entries.startswith(original)
    if existing:
        assert entries == original
    before = log.read_text()
    environment["EFI_TEST_MOUNT_FAILURE"] = "1"
    result = subprocess.run(
        [script, "configure"], check=False, env=environment, capture_output=True
    )
    assert result.returncode != 0
    assert log.read_text() == before
