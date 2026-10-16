"""
Install the boot Debian package into the target with its EFI partition mounted.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path

from .. import media
from ..errors import BuildError
from . import config
from .context import Context
from .payload import validate_payload


def run(ctx: Context) -> None:
    """
    Mount the ESP and chroot filesystems, install the boot package, and verify the UKI.
    """
    if ctx.dry_run:
        print("dry run: this step would modify the target; skipping execution")
        return
    ctx.require_apply()
    print("==>", "4/5 uki")
    ctx.uki_rel = config.FALLBACK_EFI_PATH
    subprocess.run(["mkdir", "-p", str(ctx.esp_mount)], check=True)
    subprocess.run(["mount", ctx.part_esp, str(ctx.esp_mount)], check=True)
    if ctx.source_uki is None or ctx.source_boot_deb is None:
        raise BuildError("no validated installed boot package was located")
    # Recheck all payload checksums immediately before invoking package scripts.
    if validate_payload(ctx.source_boot_deb.parent) != ctx.release:
        raise BuildError("installed boot package release changed during installation")
    mounted = []
    try:
        # Recursive binds expose device nodes and EFI variables inside the chroot.
        # Slave propagation prevents target cleanup from unmounting live filesystems.
        for name in ("dev", "proc", "sys"):
            destination = ctx.target / name
            destination.mkdir(parents=True, exist_ok=True)
            subprocess.run(["mount", "--rbind", f"/{name}", destination], check=True)
            mounted.append(destination)
            subprocess.run(["mount", "--make-rslave", destination], check=True)
        staging = ctx.target / "tmp"
        staging.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="boot-package-", dir=staging) as scratch:
            package = Path(scratch) / "boot-installed.deb"
            shutil.copyfile(ctx.source_boot_deb, package)
            subprocess.run(
                [
                    "chroot",
                    ctx.target,
                    "/usr/bin/env",
                    "UBUNTU_UKI_REGISTER_EFI=1",
                    f"UBUNTU_UKI_EFI_DISK={ctx.root_disk}",
                    "UBUNTU_UKI_EFI_PART=1",
                    "dpkg",
                    "-i",
                    str(Path("/") / package.relative_to(ctx.target)),
                ],
                check=True,
            )
    finally:
        for destination in reversed(mounted):
            subprocess.run(["umount", "-R", destination], check=False)
    destination = ctx.esp_mount / "EFI/BOOT/BOOTX64.EFI"
    if media.digest(destination) != media.digest(ctx.source_uki):
        raise BuildError("the packaged UKI on the target ESP differs from the source")
    if not (ctx.target / "usr/lib/modules" / ctx.release).is_dir():
        raise BuildError(f"installed runtime modules are missing for {ctx.release}")
    ctx.uki_path = destination
    print("ok", f"UKI installed: {destination}")
