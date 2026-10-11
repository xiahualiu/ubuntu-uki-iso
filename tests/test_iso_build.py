"""ISO content, boot records and installer validation without using host disks."""

import json
import shutil
import subprocess

import pytest

from helpers import make_pe
from ubuntu_uki_iso import media
from ubuntu_uki_iso.errors import BuildError
from ubuntu_uki_iso.iso import build
from ubuntu_uki_iso.iso.folders import FolderLayout
from ubuntu_uki_iso.rootfs.build import squash
from ubuntu_uki_iso.uki.config import cmdline


@pytest.fixture
def inputs(tmp_path):
    layout = FolderLayout(tmp_path)
    for mode, image, uki in (
        ("live", layout.live_rootfs, layout.live_uki),
        ("installed", layout.installed_rootfs, layout.installed_uki),
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
    return layout


def payload(layout):
    directory = layout.installed_rootfs.parent
    shutil.copyfile(layout.installed_uki, directory / "uki.efi")
    manifest = {
        "version": 1,
        "release": "7.0-test",
        "sha256": {name: media.digest(directory / name) for name in ("rootfs.squashfs", "uki.efi")},
    }
    (directory / "manifest.json").write_text(json.dumps(manifest))
    return directory


def test_payload_validates_matching_modules_and_cmdline(inputs):
    assert media.validate_payload(payload(inputs)) == "7.0-test"


@pytest.mark.parametrize("name", ["rootfs.squashfs", "uki.efi"])
def test_changed_payload_is_rejected(inputs, name):
    directory = payload(inputs)
    with (directory / name).open("ab") as stream:
        stream.write(b"changed")
    with pytest.raises(BuildError, match="checksum mismatch"):
        media.validate_payload(directory)


def test_rootfs_release_mismatch_is_rejected(inputs):
    with pytest.raises(BuildError, match="does not match"):
        media.validate_rootfs(inputs.installed_rootfs, "wrong", "installed")


def test_wrong_boot_mode_is_rejected(inputs):
    with pytest.raises(BuildError, match="command line"):
        media.validate_uki(inputs.live_uki, "installed")


def test_iso_rejects_mismatch_before_replacing_output(inputs):
    inputs.iso.write_bytes(b"previous")
    inputs.installed_uki.write_bytes(
        make_pe(
            {
                ".linux": b"different",
                ".initrd": b"initrd",
                ".cmdline": cmdline("installed").encode(),
                ".uname": b"7.0-test",
            }
        )
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


def test_installer_rejects_corrupt_payload_before_disk_commands(inputs, runner, console):
    from ubuntu_uki_iso.installer.context import Context
    from ubuntu_uki_iso.installer.preflight import RaidMode
    from ubuntu_uki_iso.installer.target import find_payload

    directory = payload(inputs)
    (directory / "manifest.json").write_text("{}")
    ctx = Context(
        "/dev/imaginary",
        [],
        runner,
        console,
        RaidMode.NONE,
        payload=directory / "rootfs.squashfs",
    )
    with pytest.raises(BuildError, match="invalid installation payload"):
        find_payload(ctx)
    assert runner.executed == []


def test_installer_copies_standalone_uki_without_packages(inputs, runner, console, monkeypatch):
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
        runner,
        console,
        RaidMode.NONE,
        payload=directory / "rootfs.squashfs",
    )
    ctx.derive_partitions()
    target.find_payload(ctx)
    uki.run(ctx)
    assert media.digest(mount / "boot/efi/EFI/BOOT/BOOTX64.EFI") == media.digest(
        directory / "uki.efi"
    )
    assert all(
        command[0] not in ("apt-get", "dpkg", "chroot", "dracut", "ukify")
        for command in runner.executed
    )


def test_rootfs_without_matching_modules_is_rejected(inputs):
    shutil.rmtree(inputs.root / "installed/usr/lib/modules")
    squash(inputs.root / "installed", inputs.installed_rootfs, 1)
    with pytest.raises(BuildError, match="no runtime modules"):
        media.validate_rootfs(inputs.installed_rootfs, "7.0-test", "installed")


def test_installer_extracts_rootfs_and_writes_target_fstab(inputs, runner, console, monkeypatch):
    from ubuntu_uki_iso.installer import context, target
    from ubuntu_uki_iso.installer.context import Context
    from ubuntu_uki_iso.installer.preflight import RaidMode

    directory = payload(inputs)
    mount = inputs.root / "empty-target"
    mount.mkdir()
    monkeypatch.setattr(context, "TARGET_MOUNT", mount)
    ctx = Context(
        "/dev/imaginary", [], runner, console, RaidMode.NONE, payload=directory / "rootfs.squashfs"
    )
    ctx.derive_partitions()
    runner.stub(f"blkid -s UUID -o value {ctx.part_root}", stdout="root-uuid")
    runner.stub(f"blkid -s UUID -o value {ctx.part_esp}", stdout="esp-uuid")
    record = runner.run

    def run(*argv, **kwargs):
        result = record(*argv, **kwargs)
        if argv[0] == "unsquashfs":
            subprocess.run(argv, check=True, capture_output=True)
        return result

    monkeypatch.setattr(runner, "run", run)
    target.find_payload(ctx)
    target.run(ctx)
    assert (mount / "usr/lib/modules/7.0-test/kernel/test.ko").read_bytes() == b"module"
    fstab = (mount / "etc/fstab").read_text()
    assert "UUID=root-uuid" in fstab
    assert "UUID=esp-uuid" in fstab
    assert (mount / "etc/kernel/cmdline").read_text() == cmdline("installed")
