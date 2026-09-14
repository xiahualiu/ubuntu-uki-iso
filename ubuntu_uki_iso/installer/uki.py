"""Install the kernel on the target, and let it build its own UKI.

This step does not build a UKI. It installs the kernel packages the medium
carries — with apt, in a chroot — and the package's own maintainer scripts run
``kernel-install``, which with ``layout=uki`` runs dracut and ukify and writes
the UKI to the ESP. Three consequences, and they are the whole point:

* the initramfs is host-only, because dracut runs here on the real hardware and
  includes exactly the drivers this machine needs to find its own root
  filesystem
* the install *is* a kernel upgrade. The first one on this machine is the one
  that installed it, so the mechanism that will run on every future upgrade has
  been exercised before there is anything to lose
* the machine can be given a new kernel later by handing it a newer pair of
  packages, with no installer and no ISO involved

The ESP is formatted here, which destroys whatever loader was on it — on this
machine, shim and GRUB. After that the only ways to boot are the NVRAM entry and
``\\EFI\\BOOT\\BOOTX64.EFI``. The kernel-install plugin
(:mod:`ubuntu_uki_iso.ukis.fallback`) writes the second one as part of the
package install, so the machine is bootable before the firmware entry even
exists.
"""

from __future__ import annotations

import shutil
from pathlib import Path

from .. import pe, settings
from ..errors import BuildError
from .context import Context

#: Where the packages are staged inside the target for apt to install. Under
#: /var/tmp because that is where a package manager's scratch belongs, and
#: because a failed run leaving files there is obvious rather than subtle.
DEB_STAGING = Path("var/tmp/kernel-debs")


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
        packages = ", ".join(deb.name for deb in ctx.debs) or "<the kernel packages>"
        console.grey(f"would copy into {ctx.target}: {packages}")
        console.grey(f"would chroot {ctx.target} and run `apt-get install` on them")
        console.grey("  -> the kernel's postinst runs kernel-install, which runs dracut")
        console.grey("     and ukify, and 90-uki-copy.install places the UKI at /EFI/Linux/")
        console.grey(f"  -> 95-ubuntu-uki-iso-fallback copies it to {ctx.uki_rel}")
        return

    if not ctx.release:
        raise BuildError("step_rootfs did not record a kernel release")
    if not ctx.debs:
        raise BuildError("no kernel packages were found on the medium")
    if not ctx.target.joinpath("usr/local/bin/ubuntu-uki-iso").is_file():
        raise BuildError(
            "the target has no /usr/local/bin/ubuntu-uki-iso.\n"
            "The kernel-install plugins are Python and call into that package; "
            "without it the UKI is built but the fallback is never written."
        )

    _install_kernel_packages(ctx)
    _verify(ctx)


def _install_kernel_packages(ctx: Context) -> None:
    """Install the kernel on the target the way its upgrades will.

    ``KERNEL_INSTALL_BOOT_ROOT`` is what points kernel-install at the ESP
    instead of at ``/boot`` on the root filesystem, where firmware would never
    look for it. It is set in the environment, and inherited by apt, by dpkg,
    and by the maintainer script that finally runs kernel-install — which is why
    it is set here rather than passed to kernel-install directly: kernel-install
    is no longer ours to call.
    """
    console, runner = ctx.console, ctx.runner

    staging = ctx.target / DEB_STAGING
    staging.mkdir(parents=True, exist_ok=True)
    for deb in ctx.debs:
        shutil.copyfile(deb, staging / deb.name)

    _mount_chroot_fs(ctx)
    console.info(f"installing the kernel package on the target ({ctx.release})")
    console.info("its postinst builds the initramfs — this takes a few minutes")

    result = runner.run(
        "chroot",
        str(ctx.target),
        "env",
        "KERNEL_INSTALL_BOOT_ROOT=/boot/efi",
        "DEBIAN_FRONTEND=noninteractive",
        "apt-get",
        "install",
        "-y",
        "--no-install-recommends",
        *[f"/{DEB_STAGING}/{deb.name}" for deb in ctx.debs],
        check=False,
    )
    shutil.rmtree(staging, ignore_errors=True)

    if not result.ok:
        raise BuildError(
            "installing the kernel packages failed inside the target.\n\n"
            "The existing bootloader is untouched and the ESP was just formatted,\n"
            "so the machine's bootability is now down to what this step did: see\n"
            "the output above before rebooting it."
        )


def _mount_chroot_fs(ctx: Context) -> None:
    """Mount /dev, /proc, /sys and /run so apt and dracut have what they need.

    More is mounted here than the old kernel-install call needed: apt wants
    /proc and /dev for dpkg's maintainer scripts, and dracut's initramfs build
    reads /sys to decide which modules this machine actually needs.
    """
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
    """Check the ESP itself, not apt's exit code.

    The failure this catches is a UKI that was built but placed somewhere the
    firmware will never look.
    """
    console = ctx.console

    versioned = sorted((ctx.esp_mount / "EFI/Linux").glob("*.efi"))
    if not versioned:
        raise BuildError(
            f"the kernel package installed, but {ctx.esp_mount}/EFI/Linux "
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
    _verify_contents(ctx, uki_path)
    _verify_kernel_installed(ctx)


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


def _verify_contents(ctx: Context, uki_path: Path) -> None:
    """Read the finished UKI's sections back out of the file.

    The command line is the one that matters: it is the only way to confirm the
    thing that will actually run carries the root UUID the filesystem just got,
    rather than the placeholder the payload was built with. The initramfs is
    checked alongside it because a UKI without one boots to an empty panic.
    """
    if pe.read_section(uki_path, ".initrd") is None:
        raise BuildError(
            "the UKI has no .initrd section — dracut did not contribute one, so the\n"
            "kernel would panic before it could find its root filesystem."
        )

    embedded = pe.cmdline(uki_path)
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


def _verify_kernel_installed(ctx: Context) -> None:
    """Confirm the package installed, rather than merely unpacking.

    ``/lib/modules/<release>`` exists only if dpkg ran the install through to
    the end, and the kernel-install run that produced the UKI is part of the
    same maintainer script. It is worth checking here because the payload has no
    kernel of its own: there is no second copy on this machine to fall back to.
    """
    modules = ctx.target / "lib/modules" / ctx.release
    if not modules.is_dir():
        raise BuildError(
            f"the kernel package did not install: {modules} is missing.\n"
            "The UKI on the ESP would have no modules to load."
        )
    ctx.console.ok(f"kernel installed: /lib/modules/{ctx.release}")
