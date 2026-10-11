"""Rootfs isolation, runtime modules and actual SquashFS artifacts."""

import os
import shutil
import subprocess
import sys
from pathlib import Path
from unittest.mock import Mock

import pytest

from ubuntu_uki_iso.errors import BuildError
from ubuntu_uki_iso.rootfs import build, config
from ubuntu_uki_iso.rootfs.folders import FolderLayout


def bootstrap_tree(root):
    (root / "usr/lib").mkdir(parents=True)
    (root / "usr/lib/os-release").write_text("ID=ubuntu\n")
    (root / "lib").symlink_to("usr/lib")
    (root / "etc/ssh").mkdir(parents=True)
    (root / "etc/machine-id").write_text("host-machine-id")
    (root / "etc/ssh/ssh_host_ed25519_key").write_text("shared-key")
    (root / "etc/ssh/ssh_host_ed25519_key.pub").write_text("shared-public-key")
    (root / "etc/resolv.conf").write_text("nameserver 1.2.3.4\n")


@pytest.mark.parametrize("mode", ["live", "installed"])
def test_build_bootstraps_copies_modules_and_customizes(tmp_path, monkeypatch, mode):
    deb = tmp_path / "image.deb"
    deb.touch()
    commands = []

    def run(argv, **kwargs):
        args = [str(arg) for arg in argv]
        commands.append(args)
        assert kwargs["check"] is True
        if args[0] == "mksquashfs":
            assert "SOURCE_DATE_EPOCH" not in kwargs["env"]
        else:
            assert kwargs == {"check": True}
        if args[0] == "mmdebstrap":
            assert "--mode=root" in args
            assert "--variant=minbase" in args
            bootstrap_tree(Path(args[-2]))
        elif args[0] == "dpkg-deb":
            root = Path(args[-1])
            (root / "boot").mkdir(parents=True)
            (root / "boot/vmlinuz-7.0-test").touch()
            modules = root / "lib/modules/7.0-test"
            modules.mkdir(parents=True)
            (modules / "nvme.ko").write_bytes(b"module")
            (modules / "build").symlink_to("/usr/src/build-only-headers")
        elif args[0] == "mksquashfs":
            Path(args[2]).write_bytes(b"verified image")
        return subprocess.CompletedProcess(args, 0)

    monkeypatch.setattr(build.subprocess, "run", run)
    output = build.build(deb, tmp_path / "out", mode, 2)
    layout = FolderLayout(tmp_path / "out")
    root = layout.rootfs(mode)
    assert output == layout.squashfs(mode)
    assert output.read_bytes() == b"verified image"
    assert (root / "usr/lib/modules/7.0-test/nvme.ko").read_bytes() == b"module"
    assert not (root / "usr/lib/modules/7.0-test/build").is_symlink()
    assert not (root / "boot/vmlinuz-7.0-test").exists()
    assert (root / "etc/machine-id").read_text() == ""
    assert not list((root / "etc/ssh").glob("ssh_host_*"))
    assert (root / "etc/resolv.conf").is_symlink()
    units = next(args for args in commands if args[0] == "systemctl")
    assert units[1].startswith("--root=")
    if mode == "installed":
        assert "ssh-host-keys.service" in units
        assert not (root / "etc/systemd/system/getty@tty1.service.d/autologin.conf").exists()
        assert "root=PARTLABEL=" in (root / "etc/kernel/cmdline").read_text()
    else:
        assert (root / "etc/systemd/system/getty@tty1.service.d/autologin.conf").is_file()
    runtime = root / "usr/local/lib/ubuntu-uki-iso"
    if mode == "live":
        assert (root / "usr/local/bin/ubuntu-uki-install").is_file()
        assert not (runtime / "ubuntu_uki_iso/kernel").exists()
        assert not (runtime / "ubuntu_uki_iso/uki/build.py").exists()
        monkeypatch.undo()
        result = subprocess.run(
            [
                sys.executable,
                "-I",
                "-c",
                "import sys; sys.path.insert(0, sys.argv[1]); "
                "from ubuntu_uki_iso.installer.cli import main; main(['--help'])",
                str(runtime),
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        assert "--root-disk" in result.stdout
    else:
        assert not runtime.exists()
    assert not list(layout.root.glob(f"rootfs-{mode}-*"))


@pytest.mark.parametrize("mode", ["live", "installed"])
def test_profiles_do_not_install_boot_build_tools(mode):
    packages = set(config.package_list(mode))
    assert not packages & {"dracut", "systemd-ukify", "systemd-boot-efi"}
    assert {"kmod", "systemd", "systemd-resolved"} <= packages


@pytest.mark.skipif(shutil.which("mksquashfs") is None, reason="squashfs-tools required")
def test_real_squashfs_preserves_metadata_and_excludes_transient_contents(tmp_path, monkeypatch):
    source = tmp_path / "rootfs"
    source.mkdir()
    for directory in ("proc", "sys", "dev", "run", "tmp", "var/tmp", "boot/efi"):
        (source / directory).mkdir(parents=True, exist_ok=True)
        (source / directory / "runtime-file").write_text("exclude me")
    (source / "etc").mkdir()
    file = source / "etc/test"
    file.write_text("preserve me")
    file.chmod(0o640)
    monkeypatch.setenv("SOURCE_DATE_EPOCH", "1700000000")
    output = build.squash(source, tmp_path / "one.squashfs", 2)
    second = build.squash(source, tmp_path / "two.squashfs", 2)
    assert output.read_bytes() == second.read_bytes()
    extracted = tmp_path / "extracted"
    subprocess.run(
        ["unsquashfs", "-d", str(extracted), str(output)], check=True, capture_output=True
    )
    result = extracted / "etc/test"
    assert result.read_text() == "preserve me"
    assert result.stat().st_mode & 0o777 == 0o640
    assert result.stat().st_uid == os.getuid()
    for directory in ("proc", "sys", "dev", "run", "tmp", "var/tmp", "boot/efi"):
        assert (extracted / directory).is_dir()
        assert not list((extracted / directory).iterdir())


def test_compression_failure_preserves_previous_output(tmp_path, monkeypatch):
    source = tmp_path / "rootfs"
    source.mkdir()
    output = tmp_path / "image.squashfs"
    output.write_bytes(b"previous")
    monkeypatch.setattr(
        build.subprocess, "run", Mock(side_effect=subprocess.CalledProcessError(1, ["mksquashfs"]))
    )
    with pytest.raises(subprocess.CalledProcessError):
        build.squash(source, output, 1)
    assert output.read_bytes() == b"previous"
    assert not list(tmp_path.glob("squashfs-*"))


def test_cli_builds_both_images_with_same_input(tmp_path, monkeypatch):
    deb = tmp_path / "kernel.deb"
    monkeypatch.setattr(build, "preflight", Mock())
    compile_image = Mock(return_value=tmp_path / "image.squashfs")
    monkeypatch.setattr(build, "build", compile_image)
    assert (
        build.main(
            ["--temp-dir", str(tmp_path), "--kernel-deb", str(deb), "--mode", "all", "--jobs", "2"]
        )
        == 0
    )
    assert [call.args for call in compile_image.call_args_list] == [
        (deb, tmp_path, "live", 2),
        (deb, tmp_path, "installed", 2),
    ]


def test_preflight_failure_does_not_create_directories(tmp_path, monkeypatch):
    monkeypatch.setattr(build, "preflight", Mock(side_effect=BuildError("missing mmdebstrap")))
    directory = tmp_path / "new"
    assert build.main(["--temp-dir", str(directory)]) == 1
    assert not directory.exists()
