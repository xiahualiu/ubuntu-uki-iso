"""
Shared paths and validation for boot packages consumed by rootfs and ISO builds.
"""

import subprocess
from pathlib import Path

from . import media
from .errors import BuildError
from .settings import Mode

EFI_PATH = Path("boot/efi/EFI/BOOT/BOOTX64.EFI")
METADATA = Path("usr/lib/ubuntu-uki-iso")


def package_path(temp_dir: Path, mode: Mode) -> Path:
    """
    Locate the boot package emitted by the UKI builder.
    """
    if mode not in (Mode.LIVE, Mode.INSTALLED):
        raise BuildError(f"unknown boot package mode: {mode}")
    return temp_dir / "uki" / f"boot-{mode}.deb"


def extract_package(package: Path, destination: Path, mode: Mode) -> str:
    """
    Extract and validate a package before rootfs installation or ISO assembly.
    """
    subprocess.run(["dpkg-deb", "--extract", package, destination], check=True)
    release = media.validate_uki(destination / EFI_PATH, mode)
    if (destination / METADATA / "release").read_text().strip() != release:
        raise BuildError("boot package release metadata does not match its UKI")
    if (destination / METADATA / "mode").read_text().strip() != mode:
        raise BuildError("boot package has the wrong mode")
    if not (destination / "usr/lib/modules" / release).is_dir():
        raise BuildError(f"boot package has no modules for {release}")
    return release
