"""
ISO content, boot records and installer validation without using host disks.
"""

import json
import shutil
import subprocess

import pytest

from helpers import make_pe
from ubuntu_uki_iso import media
from ubuntu_uki_iso.boot_package import package_path
from ubuntu_uki_iso.errors import BuildError
from ubuntu_uki_iso.installer.payload import validate_payload
from ubuntu_uki_iso.iso import build
from ubuntu_uki_iso.iso.folders import FolderLayout
from ubuntu_uki_iso.rootfs.build import squash
from ubuntu_uki_iso.settings import Mode
from ubuntu_uki_iso.uki.config import cmdline
from ubuntu_uki_iso.uki.package import create_package


@pytest.fixture
def inputs(tmp_path):
    layout = FolderLayout(tmp_path)
    for mode, image, uki in (
        (Mode.LIVE, layout.live_layer, layout.live_uki),
        (Mode.INSTALLED, layout.installed_rootfs, layout.installed_uki),
    ):
        root = tmp_path / mode
        (root / "etc/kernel").mkdir(parents=True)
        (root / "etc/kernel/cmdline").write_text(cmdline(mode))
        (root / "usr/lib/ubuntu-uki-iso").mkdir(parents=True)
        (root / "usr/lib/ubuntu-uki-iso/release").write_text("7.0-test\n")
        (root / "usr/lib/modules/7.0-test/kernel").mkdir(parents=True)
        (root / "usr/lib/modules/7.0-test/kernel/test.ko").write_bytes(b"module")
        squash(root, image, 1)
        uki.parent.mkdir(exist_ok=True)
        uki.write_bytes(
            make_pe(
                {
                    ".linux": b"kernel",
                    ".initrd": b"initrd",
                    ".cmdline": cmdline(mode).encode(),
                    ".uname": b"7.0-test\0",
                }
            )
        )
        create_package(
            uki, root / "usr/lib/modules/7.0-test", "7.0-test", mode, package_path(tmp_path, mode)
        )
    (tmp_path / "live/usr/lib/ubuntu-uki-iso/base-sha256").write_text(
        media.digest(layout.installed_rootfs) + "\n"
    )
    squash(tmp_path / "live", layout.live_layer, 1)
    return layout


def payload(layout):
    directory = layout.installed_rootfs.parent
    shutil.copyfile(layout.installed_uki, directory / "uki.efi")
    shutil.copyfile(package_path(layout.root, Mode.INSTALLED), directory / "boot-installed.deb")
    manifest = {
        "version": 2,
        "release": "7.0-test",
        "sha256": {
            name: media.digest(directory / name)
            for name in ("base.squashfs", "uki.efi", "boot-installed.deb")
        },
    }
    (directory / "manifest.json").write_text(json.dumps(manifest))
    return directory


def test_payload_validates_matching_modules_and_cmdline(inputs):
    assert validate_payload(payload(inputs)) == "7.0-test"


@pytest.mark.parametrize("name", ["base.squashfs", "uki.efi", "boot-installed.deb"])
def test_changed_payload_is_rejected(inputs, name):
    directory = payload(inputs)
    with (directory / name).open("ab") as stream:
        stream.write(b"changed")
    with pytest.raises(BuildError, match="checksum mismatch"):
        validate_payload(directory)


def test_payload_rejects_package_with_different_uki(inputs):
    directory = payload(inputs)
    different = inputs.root / "different.efi"
    different.write_bytes(
        make_pe(
            {
                ".linux": b"different-kernel",
                ".initrd": b"initrd",
                ".cmdline": cmdline(Mode.INSTALLED).encode(),
                ".uname": b"7.0-test",
            }
        )
    )
    create_package(
        different,
        inputs.root / "installed/usr/lib/modules/7.0-test",
        "7.0-test",
        Mode.INSTALLED,
        directory / "boot-installed.deb",
    )
    manifest = json.loads((directory / "manifest.json").read_text())
    manifest["sha256"]["boot-installed.deb"] = media.digest(directory / "boot-installed.deb")
    (directory / "manifest.json").write_text(json.dumps(manifest))
    with pytest.raises(BuildError, match="boot package UKI does not match"):
        validate_payload(directory)


def test_rootfs_release_mismatch_is_rejected(inputs):
    with pytest.raises(BuildError, match="does not match"):
        media.validate_rootfs(inputs.installed_rootfs, "wrong", Mode.INSTALLED)


def test_wrong_boot_mode_is_rejected(inputs):
    with pytest.raises(BuildError, match="command line"):
        media.validate_uki(inputs.live_uki, Mode.INSTALLED)


def test_iso_rejects_mismatch_before_replacing_output(inputs):
    inputs.iso.write_bytes(b"previous")
    inputs.installed_uki.write_bytes(
        make_pe(
            {
                ".linux": b"different",
                ".initrd": b"initrd",
                ".cmdline": cmdline(Mode.INSTALLED).encode(),
                ".uname": b"7.0-test",
            }
        )
    )
    create_package(
        inputs.installed_uki,
        inputs.root / "installed/usr/lib/modules/7.0-test",
        "7.0-test",
        Mode.INSTALLED,
        package_path(inputs.root, Mode.INSTALLED),
    )
    with pytest.raises(BuildError, match="different kernels"):
        build.build(inputs.root)
    assert inputs.iso.read_bytes() == b"previous"


@pytest.mark.skipif(
    any(shutil.which(tool) is None for tool in ("xorriso", "mmd", "mcopy", "mkfs.vfat")),
    reason="ISO tools required",
)
def test_real_iso_has_optical_usb_boot_and_exact_payload(inputs):
    output = build.build(inputs.root)
    assert output.is_file()
    assert not list(inputs.root.glob("iso-*"))


def test_preflight_failure_creates_no_output(tmp_path, monkeypatch):
    def fail(path):
        raise BuildError("missing packages")

    monkeypatch.setattr(build, "preflight", fail)
    assert build.main(["--temp-dir", str(tmp_path / "new")]) == 1
    assert not (tmp_path / "new").exists()


def test_installer_rejects_corrupt_payload_before_disk_commands(inputs, monkeypatch):
    from ubuntu_uki_iso.installer.context import Context
    from ubuntu_uki_iso.installer.preflight import RaidMode
    from ubuntu_uki_iso.installer.target import find_payload

    directory = payload(inputs)
    (directory / "manifest.json").write_text("{}")
    calls = []

    def no_commands(argv, **kwargs):
        calls.append(argv)
        raise AssertionError("invalid payload must be rejected before executing commands")

    monkeypatch.setattr(subprocess, "run", no_commands)
    ctx = Context(
        "/dev/imaginary",
        [],
        RaidMode.NONE,
        payload=directory / "base.squashfs",
    )
    with pytest.raises(BuildError, match="invalid installation payload"):
        find_payload(ctx)
    assert calls == []


@pytest.mark.parametrize("fail", [False, True])
def test_installer_installs_boot_package_in_target_chroot(inputs, monkeypatch, fail):
    from ubuntu_uki_iso.installer import context, target, uki
    from ubuntu_uki_iso.installer.context import Context
    from ubuntu_uki_iso.installer.preflight import RaidMode

    directory = payload(inputs)
    mount = inputs.root / "target"
    monkeypatch.setattr(context, "TARGET_MOUNT", mount)
    (mount / "usr/lib/modules/7.0-test").mkdir(parents=True)
    ctx = Context(
        "/dev/imaginary",
        [],
        RaidMode.NONE,
        payload=directory / "base.squashfs",
    )
    ctx.derive_partitions()
    target.find_payload(ctx)
    original = subprocess.run
    commands = []
    ctx.dry_run = False
    ctx.confirmed = True

    def run(argv, **kwargs):
        args = [str(arg) for arg in argv]
        if args[0] in ("dpkg-deb", "unsquashfs"):
            return original(argv, **kwargs)
        commands.append(args)
        if args[0] == "chroot":
            if fail:
                raise BuildError("package installation failed")
            # Model dpkg unpacking the real package without running firmware tools.
            original(["dpkg-deb", "--extract", directory / "boot-installed.deb", mount], check=True)
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(subprocess, "run", run)
    if fail:
        with pytest.raises(BuildError, match="package installation failed"):
            uki.run(ctx)
        assert [command[-1] for command in commands if command[0] == "umount"] == [
            str(mount / name) for name in ("sys", "proc", "dev")
        ]
        assert not list((mount / "tmp").iterdir())
        return
    uki.run(ctx)
    assert not any(command[0].startswith("mkfs") for command in commands)
    install = next(command for command in commands if command[0] == "chroot")
    assert install[1:4] == [str(mount), "/usr/bin/env", "UBUNTU_UKI_REGISTER_EFI=1"]
    assert "UBUNTU_UKI_EFI_DISK=/dev/imaginary" in install
    assert install[-3:-1] == ["dpkg", "-i"]
    unmounts = [command[-1] for command in commands if command[0] == "umount"]
    assert unmounts == [str(mount / name) for name in ("sys", "proc", "dev")]
    assert media.digest(mount / "boot/efi/EFI/BOOT/BOOTX64.EFI") == media.digest(
        directory / "uki.efi"
    )
    assert all(
        command[0] not in ("apt-get", "dracut", "ukify", "efibootmgr") for command in commands
    )


def test_rootfs_without_matching_modules_is_rejected(inputs):
    shutil.rmtree(inputs.root / "installed/usr/lib/modules")
    squash(inputs.root / Mode.INSTALLED, inputs.installed_rootfs, 1)
    with pytest.raises(BuildError, match="no runtime modules"):
        media.validate_rootfs(inputs.installed_rootfs, "7.0-test", Mode.INSTALLED)


def test_installer_extracts_rootfs_and_writes_target_fstab(inputs, monkeypatch):
    from ubuntu_uki_iso.installer import context, target
    from ubuntu_uki_iso.installer.context import Context
    from ubuntu_uki_iso.installer.preflight import RaidMode

    directory = payload(inputs)
    mount = inputs.root / "empty-target"
    mount.mkdir()
    monkeypatch.setattr(context, "TARGET_MOUNT", mount)
    ctx = Context(
        "/dev/imaginary",
        [],
        RaidMode.NONE,
        payload=directory / "base.squashfs",
        dry_run=False,
        confirmed=True,
    )
    ctx.derive_partitions()
    original = subprocess.run
    commands = []

    def run(argv, **kwargs):
        args = [str(arg) for arg in argv]
        commands.append(args)
        if args[0] in ("unsquashfs", "dpkg-deb"):
            return original(argv, **kwargs)
        stdout = ""
        if args[0] == "blkid":
            stdout = "root-uuid" if args[-1] == ctx.part_root else "esp-uuid"
        return subprocess.CompletedProcess(args, 0, stdout, "")

    monkeypatch.setattr(subprocess, "run", run)
    target.find_payload(ctx)
    target.run(ctx)
    formatted = next(i for i, command in enumerate(commands) if command[0] == "mkfs.vfat")
    probed = next(
        i
        for i, command in enumerate(commands)
        if command[0] == "blkid" and command[-1] == ctx.part_esp
    )
    assert formatted < probed
    assert (mount / "usr/lib/modules/7.0-test/kernel/test.ko").read_bytes() == b"module"
    fstab = (mount / "etc/fstab").read_text()
    assert "UUID=root-uuid" in fstab
    assert "UUID=esp-uuid" in fstab
    assert (mount / "etc/kernel/cmdline").read_text() == cmdline(Mode.INSTALLED)


def test_iso_rejects_live_layer_from_another_base(inputs):
    inputs.iso.write_bytes(b"previous")
    with inputs.installed_rootfs.open("ab") as stream:
        stream.write(b"different base image")
    with pytest.raises(BuildError, match="different shared base"):
        build.build(inputs.root)
    assert inputs.iso.read_bytes() == b"previous"
