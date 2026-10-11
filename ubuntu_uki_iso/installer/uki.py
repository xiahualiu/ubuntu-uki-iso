"""Copy the prebuilt installed UKI onto the target EFI System Partition."""

from __future__ import annotations

import shutil

from .. import media, settings
from ..errors import BuildError
from .context import Context


def run(ctx: Context) -> None:
    """Format and mount the target ESP, copy the UKI, and verify its contents."""
    console, runner = ctx.console, ctx.runner
    console.step("4/5 uki")
    ctx.uki_rel = settings.FALLBACK_EFI_PATH
    runner.destructive(
        f"format {ctx.part_esp} as FAT32",
        "mkfs.vfat",
        "-F",
        "32",
        "-n",
        settings.ESP_LABEL,
        ctx.part_esp,
    )
    runner.run("mkdir", "-p", str(ctx.esp_mount), check=True)
    runner.run("mount", ctx.part_esp, str(ctx.esp_mount), check=True)
    if ctx.dry_run:
        console.grey(f"would copy {ctx.source_uki} to {ctx.esp_mount}/EFI/BOOT/BOOTX64.EFI")
        return
    if ctx.source_uki is None:
        raise BuildError("no validated installed UKI was located")
    if media.validate_uki(ctx.source_uki, "installed") != ctx.release:
        raise BuildError("installed UKI release changed during installation")
    destination = ctx.esp_mount / "EFI/BOOT/BOOTX64.EFI"
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(ctx.source_uki, destination)
    if media.digest(destination) != media.digest(ctx.source_uki):
        raise BuildError("the UKI copy on the target ESP differs from the source")
    if not (ctx.target / "usr/lib/modules" / ctx.release).is_dir():
        raise BuildError(f"installed runtime modules are missing for {ctx.release}")
    ctx.uki_path = destination
    console.ok(f"UKI installed: {destination}")
