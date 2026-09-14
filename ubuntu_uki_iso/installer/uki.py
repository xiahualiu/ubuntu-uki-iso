"""Generate the UKI on the target and put it on the ESP.

This runs ``kernel-install`` *inside the installed system*, in a chroot, on the
real hardware. Two consequences, and they are the whole point:

* the initramfs is host-only, so dracut includes the drivers this machine
  actually needs to find its own root filesystem
* the UKI is assembled by the same path that will assemble it on every future
  kernel upgrade, so that mechanism is exercised now rather than discovered
  later

The ESP is formatted here, which destroys whatever loader was on it — on this
machine, shim and GRUB. After that, the only ways to boot are the NVRAM entry
and ``\\EFI\\BOOT\\BOOTX64.EFI``. The kernel-install plugin
(:mod:`ubuntu_uki_iso.ukis.fallback`) writes the second one as part of this step, so
the machine is bootable before the firmware entry even exists.
"""

from __future__ import annotations

from pathlib import Path

from .. import pe, settings
from ..errors import BuildError
from .context import Context


def run(ctx: Context) -> None:
    console, runner = ctx.console, ctx.runner
    console.step("4/5 uki")

    runner.destructive(
        f"format {ctx.part_esp} as FAT32",
        "mkfs.vfat",
        "-F",
        "32",
        "-n",
        settings.ESP_LABEL,
        ctx.part_esp,
    )

    runner.run("mkdir", "-p", str(ctx.esp_mount))
    runner.run("mount", ctx.part_esp, str(ctx.esp_mount))

    if ctx.dry_run:
        # Context.uki_rel has to be set even in a dry run, or the plan cannot
        # name the firmware entry — and a dry run is the default, so that would
        # be the only behaviour anyone ever saw.
        ctx.uki_rel = settings.FALLBACK_EFI_PATH
        release = ctx.release or "<release>"
        console.grey(f"would chroot {ctx.target} and run:")
        console.grey(f"  kernel-install add {release} /boot/vmlinuz-{release}")
        console.grey("  -> dracut builds the initramfs, ukify builds the UKI,")
        console.grey("     90-uki-copy.install places it at /EFI/Linux/")
        console.grey(f"  -> 95-ubuntu-uki-iso-fallback copies it to {ctx.uki_rel}")
        return

    if not ctx.release:
        raise BuildError("step_rootfs did not record a kernel release")
    if not ctx.target.joinpath("usr/local/bin/ubuntu-uki-iso").is_file():
        raise BuildError(
            "the target has no /usr/local/bin/ubuntu-uki-iso.\n"
            "The kernel-install plugins are Python and call into that package; "
            "without it the UKI is built but the fallback is never written."
        )

    _mount_chroot_fs(ctx)
    console.info("running kernel-install inside the target (dracut + ukify)")
    console.info("this builds the initramfs for this machine — it takes a few minutes")

    result = runner.run(
        "chroot",
        str(ctx.target),
        "env",
        "KERNEL_INSTALL_BOOT_ROOT=/boot/efi",
        "kernel-install",
        "add",
        ctx.release,
        f"/boot/vmlinuz-{ctx.release}",
        check=False,
    )
    if not result.ok:
        raise BuildError(
            "kernel-install failed inside the target.\n"
            "The existing bootloader is untouched, so the machine still boots; "
            "see the output above for the cause."
        )

    _verify(ctx)


def _mount_chroot_fs(ctx: Context) -> None:
    runner = ctx.runner
    for source, target, fstype in (
        ("/dev", ctx.target / "dev", None),
        ("proc", ctx.target / "proc", "proc"),
        ("sys", ctx.target / "sys", "sysfs"),
        ("/run", ctx.target / "run", None),
    ):
        target.mkdir(parents=True, exist_ok=True)
        argv = ["mount"]
        argv += ["-t", fstype] if fstype else ["--bind"]
        argv += [source, str(target)]
        runner.run(*argv, check=False)


def _verify(ctx: Context) -> None:
    """Check the ESP itself, not kernel-install's exit code.

    The failure this catches is a UKI that was built but placed somewhere the
    firmware will never look.
    """
    console = ctx.console

    versioned = sorted((ctx.esp_mount / "EFI/Linux").glob("*.efi"))
    if not versioned:
        raise BuildError(
            f"kernel-install reported success but {ctx.esp_mount}/EFI/Linux "
            "contains no .efi\n\n"
            "The UKI is what the firmware boots. Without it the machine has no\n"
            "bootable entry — though \\EFI\\BOOT\\BOOTX64.EFI may still be present."
        )

    # Held in a local as well as on the context: `uki_path` is `Path | None` on
    # the dataclass, and every check below needs the non-None guarantee this
    # assignment just established.
    uki_path = versioned[0]
    ctx.uki_path = uki_path
    ctx.uki_rel = settings.FALLBACK_EFI_PATH
    console.ok(
        f"UKI: {uki_path.relative_to(ctx.esp_mount)} ({uki_path.stat().st_size // 1024 // 1024} MB)"
    )

    _verify_fallback(ctx, uki_path)
    _verify_cmdline(ctx)


def _verify_fallback(ctx: Context, uki_path: Path) -> None:
    """Confirm the kernel-install plugin wrote the firmware fallback.

    This is the file that makes the machine bootable without NVRAM, and the
    only one that survives a CMOS reset — so its absence is fatal here rather
    than a warning.
    """
    fallback = ctx.esp_mount / "EFI/BOOT/BOOTX64.EFI"
    if not fallback.is_file():
        raise BuildError(
            f"the kernel-install fallback plugin did not write {fallback}.\n\n"
            "That file is how firmware boots this machine with no configuration, "
            "and — because this step formats the ESP — it is also the only boot\n"
            "path that does not depend on the NVRAM entry efibootmgr is about to\n"
            "create. Check that /etc/kernel/install.d/95-ubuntu-uki-iso-fallback.install\n"
            "ran and that python3 is present in the target."
        )

    if fallback.stat().st_size != uki_path.stat().st_size:
        raise BuildError(
            f"{fallback} is {fallback.stat().st_size} bytes but the UKI is "
            f"{uki_path.stat().st_size} — the copy is truncated"
        )
    ctx.console.ok("firmware fallback written: \\EFI\\BOOT\\BOOTX64.EFI")


def _verify_cmdline(ctx: Context) -> None:
    """Read the embedded command line back out of the finished UKI.

    The only way to confirm the thing that will actually run has the root UUID
    the filesystem just got, rather than the placeholder it was built with.
    """
    embedded = pe.cmdline(ctx.uki_path) if ctx.uki_path else None
    if embedded is None:
        ctx.console.warn("could not read .cmdline out of the UKI; skipping that check")
        return

    ctx.console.info(f"embedded cmdline: {embedded}")

    if "@@ROOT_UUID@@" in embedded:
        raise BuildError(
            "the UKI's command line still contains @@ROOT_UUID@@ — it would panic at\n"
            "boot looking for a filesystem that does not exist."
        )
    if f"root=UUID={ctx.root_uuid}" not in embedded:
        raise BuildError(
            f"the UKI's command line does not contain root=UUID={ctx.root_uuid}\n"
            f"It says: {embedded}"
        )
    ctx.console.ok("the UKI points at the filesystem that was just created")
