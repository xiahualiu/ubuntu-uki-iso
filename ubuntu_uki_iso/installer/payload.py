"""
Validate the installed payload before allowing target disk writes.
"""

import json
import subprocess
import tempfile
from pathlib import Path

from ..boot_package import EFI_PATH, extract_package
from ..errors import BuildError
from ..media import digest, validate_rootfs, validate_uki
from ..settings import Mode


def validate_payload(directory: Path) -> str:
    """
    Validate installed artifacts and manifest before allowing disk writes.
    """
    rootfs, uki = directory / "base.squashfs", directory / "uki.efi"
    try:
        manifest = json.loads((directory / "manifest.json").read_text())
        release = validate_uki(uki, Mode.INSTALLED)
        if manifest["version"] != 2 or manifest["release"] != release:
            raise BuildError("payload manifest has an incompatible version or kernel release")
        for path in (rootfs, uki, directory / "boot-installed.deb"):
            if digest(path) != manifest["sha256"][path.name]:
                raise BuildError(f"payload checksum mismatch: {path.name}")
        with tempfile.TemporaryDirectory(prefix="validate-boot-") as scratch:
            extracted = Path(scratch)
            if (
                extract_package(directory / "boot-installed.deb", extracted, Mode.INSTALLED)
                != release
            ):
                raise BuildError("boot package release does not match the payload")
            if digest(extracted / EFI_PATH) != digest(uki):
                raise BuildError("boot package UKI does not match the payload UKI")
        validate_rootfs(rootfs, release, Mode.INSTALLED)
        return release
    except (OSError, ValueError, KeyError, TypeError, subprocess.CalledProcessError) as exc:
        raise BuildError(f"invalid installation payload in {directory}: {exc}") from exc
