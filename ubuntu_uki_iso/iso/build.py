"""
Assemble a hybrid UEFI ISO from a shared base, live layer and two UKIs.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from .. import gpt, media, pe, settings
from ..boot_package import EFI_PATH, extract_package, package_path
from ..errors import BuildError, ConfigError
from ..settings import Mode
from . import config
from .folders import FolderLayout
from .validation import parse_el_torito


def read_kernel_hash(path: Path) -> str:
    """
    Return the embedded kernel hash for comparing live and installed UKIs.
    """
    raw = pe.read_section(path, ".linux")
    if not raw:
        raise BuildError(f"{path} has no kernel")
    return hashlib.sha256(raw).hexdigest()


def preflight(temp_dir: Path) -> None:
    """
    Check ISO dependencies and space before creating any build files.
    """
    packages = config.required_packages()
    result = subprocess.run(
        ["apt", "list", "--installed", *packages], check=True, capture_output=True, text=True
    )
    installed = {line.partition("/")[0] for line in result.stdout.splitlines() if "/" in line}
    missing = [package for package in packages if package not in installed]
    if missing:
        raise BuildError(
            f"missing packages: {', '.join(missing)}\n"
            f"install with: apt-get install -y {' '.join(missing)}"
        )
    layout = FolderLayout(temp_dir)
    inputs = (
        layout.live_layer,
        layout.installed_rootfs,
        package_path(layout.root, Mode.LIVE),
        package_path(layout.root, Mode.INSTALLED),
    )
    for path in inputs:
        if not path.is_file():
            raise BuildError(f"missing ISO input: {path}")
    required = sum(path.stat().st_size for path in inputs) * 2 + 256 * 1024**2
    if shutil.disk_usage(layout.root).free < required:
        raise BuildError(f"insufficient free space under {temp_dir}: need {required} bytes")


def verify(iso: Path, stage: Path, live_uki: Path) -> None:
    """
    Check the volume label, optical/USB boot records and embedded file contents.
    """
    result = subprocess.run(
        ["xorriso", "-indev", iso, "-pvd_info", "-report_el_torito", "plain"],
        check=True,
        capture_output=True,
        text=True,
    )
    report = result.stdout + result.stderr
    labels = [
        line.split(":", 1)[1].strip().strip("'")
        for line in report.splitlines()
        if line.strip().startswith("Volume id")
    ]
    if settings.VOLID not in labels or not parse_el_torito(report):
        raise BuildError("ISO volume label or UEFI El Torito entry is missing")
    table = gpt.parse(iso)
    if not table.has_mbr or not table.has_gpt or table.esp() is None:
        raise BuildError("ISO has no protective MBR, GPT or EFI System Partition")
    extracted = stage.parent / "verified"
    subprocess.run(
        ["xorriso", "-osirrox", "on", "-indev", iso, "-extract", "/", extracted],
        check=True,
        capture_output=True,
    )
    for relative in (
        "live/live-overlay.squashfs",
        "payload/base.squashfs",
        "payload/uki.efi",
        "payload/boot-installed.deb",
        "payload/manifest.json",
        "boot/efi.img",
    ):
        if media.digest(stage / relative) != media.digest(extracted / relative):
            raise BuildError(f"ISO embedded file differs: {relative}")
    copied = stage.parent / "verified-boot.efi"
    subprocess.run(
        ["mcopy", "-i", extracted / "boot/efi.img", "::/EFI/BOOT/BOOTX64.EFI", copied], check=True
    )
    if media.digest(copied) != media.digest(live_uki):
        raise BuildError("EFI boot image does not contain the live UKI")
    esp = table.esp()
    assert esp is not None
    with iso.open("rb") as stream, (stage / "boot/efi.img").open("rb") as expected:
        stream.seek(esp.first_lba * 512)
        while block := expected.read(1024**2):
            if stream.read(len(block)) != block:
                raise BuildError("appended USB ESP differs from the optical boot image")


def build(temp_dir: Path) -> Path:
    """
    Build and verify a LiveUSB ISO from prepared rootfs and UKI artifacts.

    Args:
        temp_dir: Directory containing the rootfs and UKI builder outputs.

    Returns:
        Path to the verified ISO, replacing any previous output only on success.
    """
    layout = FolderLayout(temp_dir)
    with tempfile.TemporaryDirectory(prefix="iso-packages-", dir=layout.root) as scratch:
        for mode in (Mode.LIVE, Mode.INSTALLED):
            destination = Path(scratch) / mode
            extract_package(package_path(layout.root, mode), destination, mode)
        layout.live_uki = Path(scratch) / Mode.LIVE / EFI_PATH
        layout.installed_uki = Path(scratch) / Mode.INSTALLED / EFI_PATH
        return assemble(layout)


def assemble(layout: FolderLayout) -> Path:
    """
    Assemble an ISO using UKIs extracted from validated boot packages.
    """
    release = media.validate_uki(layout.live_uki, Mode.LIVE)
    if media.validate_uki(layout.installed_uki, Mode.INSTALLED) != release:
        raise BuildError("live and installed UKIs have different kernel releases")
    if read_kernel_hash(layout.live_uki) != read_kernel_hash(layout.installed_uki):
        raise BuildError("live and installed UKIs contain different kernels")
    media.validate_rootfs(layout.live_layer, release, Mode.LIVE)
    base_hash = subprocess.run(
        ["unsquashfs", "-cat", layout.live_layer, "usr/lib/ubuntu-uki-iso/base-sha256"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    if base_hash != media.digest(layout.installed_rootfs):
        raise BuildError("live installer layer was built against a different shared base")
    media.validate_rootfs(layout.installed_rootfs, release, Mode.INSTALLED)
    if len(settings.ESP_LABEL) > 11:
        raise BuildError("ESP label exceeds FAT's 11-character limit")
    with tempfile.TemporaryDirectory(prefix="iso-", dir=layout.root) as scratch:
        work = Path(scratch)
        stage = work / "stage"
        for relative, source in (
            ("live/live-overlay.squashfs", layout.live_layer),
            ("payload/base.squashfs", layout.installed_rootfs),
            ("payload/uki.efi", layout.installed_uki),
            ("payload/boot-installed.deb", package_path(layout.root, Mode.INSTALLED)),
        ):
            destination = stage / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, destination)
        payload = stage / "payload"
        manifest = {
            "version": 2,
            "release": release,
            "sha256": {
                name: media.digest(payload / name)
                for name in ("base.squashfs", "uki.efi", "boot-installed.deb")
            },
        }
        (payload / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
        image = stage / "boot/efi.img"
        image.parent.mkdir()
        with image.open("wb") as stream:
            stream.truncate(config.efi_image_size(layout.live_uki.stat().st_size))
        subprocess.run(["mkfs.vfat", "-F", "32", "-n", settings.ESP_LABEL, image], check=True)
        subprocess.run(["mmd", "-i", image, "::/EFI", "::/EFI/BOOT"], check=True)
        subprocess.run(
            ["mcopy", "-i", image, layout.live_uki, "::/EFI/BOOT/BOOTX64.EFI"], check=True
        )
        candidate = work / "image.iso"
        argv = [
            "xorriso",
            "-as",
            "mkisofs",
            "-volid",
            settings.VOLID,
            "-iso-level",
            "3",
            "-r",
            "-J",
            "-joliet-long",
            "-e",
            "boot/efi.img",
            "-no-emul-boot",
            "-append_partition",
            "2",
            "0xef",
            str(image),
            "-appended_part_as_gpt",
        ]
        if epoch := os.environ.get("SOURCE_DATE_EPOCH"):
            stamp = datetime.fromtimestamp(int(epoch), UTC).strftime("%Y%m%d%H%M%S00")
            argv += ["--modification-date=" + stamp]
        argv += ["-o", str(candidate), str(stage)]
        subprocess.run(argv, check=True)
        verify(candidate, stage, layout.live_uki)
        candidate.replace(layout.iso)
    return layout.iso


def main(argv: list[str] | None = None) -> int:
    """
    Parse build arguments and assemble the ISO with direct system commands.
    """
    parser = argparse.ArgumentParser(description="Build a bootable UEFI LiveUSB ISO")
    parser.add_argument("--temp-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        preflight(args.temp_dir)
        print(f"==> ISO: {build(args.temp_dir)}")
        return 0
    except (BuildError, ConfigError, OSError, ValueError, subprocess.CalledProcessError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
