"""Install the kernel on the target: one package, and the boot artifact with it.

This step installs the package the medium carries — the kernel, its modules and
the UKI, in one file — and then reads back what landed. It does not build
anything. The UKI was built on the build machine, where it could be inspected
and booted in a VM before any disk was touched, and this machine receives a
finished artifact.

Two consequences, and they are the point:

* the install is seconds rather than minutes — no dracut, no ukify, nothing
  compiled
* the install **is** an upgrade. A later kernel on this machine is a newer
  version of the same package, installed by the same command, so the path that
  runs on the first day is the path that runs on every later one

The ESP is formatted here, which destroys whatever loader was on it — on this
machine, shim and GRUB. After that the only ways to boot are the NVRAM entry
``efientry`` creates and ``\\EFI\\BOOT\\BOOTX64.EFI``. Both come from the
package's postinst (:mod:`ubuntu_uki_iso.ukis.postinst`), so the machine is
bootable before this step has finished checking it.
"""

from __future__ import annotations

import shutil
from pathlib import Path

from .. import pe, settings
from ..config import boot
from ..errors import BuildError
from .context import Context

#: Where the packages are staged inside the target for apt to install.
#:
#: On a tmpfs rather than the target's new filesystem: they are read once and
#: never needed again, and a copy that never reaches the disk is a copy a failed
#: install cannot leave behind.
DEB_STAGING = Path("var/tmp/kernel-debs")

#: The target's copy of the package's UKI, which the postinst installs from.
#: Comparing the ESP against this is what proves the copy was faithful.
PACKAGED_UKI = Path("usr/lib/ubuntu-uki-iso/uki.efi")


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
        packages = ", ".join(deb.name for deb in ctx.debs) or "<the package>"
        console.grey(f"would install into {ctx.target}: {packages}")
        console.grey(f"would chroot {ctx.target} and run `apt-get install` on it")
        console.grey("  -> its postinst places the UKI at /EFI/Linux/ and refreshes")
        console.grey(f"     {ctx.uki_rel} — the UKI is already built")
        return

    if not ctx.release:
        raise BuildError("step_rootfs did not record a kernel release")
    if not ctx.debs:
        raise BuildError("no packages were found on the medium")
    if not ctx.target.joinpath("usr/local/bin/ubuntu-uki-iso").is_file():
        raise BuildError(
            "the target has no /usr/local/bin/ubuntu-uki-iso.\n"
            "`--bless` and the boot marker both run from it, and both are recovery\n"
            "paths — a machine that cannot run them cannot be repaired from itself."
        )

    _install_package(ctx)
    _verify(ctx)


def _install_package(ctx: Context) -> None:
    """Install the package on the target, the way its upgrades will be.

    There is no ``KERNEL_INSTALL_BOOT_ROOT`` any more, because nothing here
    calls kernel-install: the postinst finds the ESP from the installed
    system's own settings, which is the same value the fstab was written with.

    The staging tmpfs is unmounted before the verification runs, so what is
    checked is the target with the install complete and nothing extra mounted
    over it.
    """
    console, runner = ctx.console, ctx.runner

    staging = ctx.target / DEB_STAGING
    staging.mkdir(parents=True, exist_ok=True)
    runner.run("mount", "-t", "tmpfs", "tmpfs", str(staging))

    try:
        for deb in ctx.debs:
            shutil.copyfile(deb, staging / deb.name)

        _mount_chroot_fs(ctx)
        console.info(f"installing the kernel package on the target ({ctx.release})")
        console.info("it carries the UKI already built — this does not take minutes")

        result = runner.run(
            "chroot",
            str(ctx.target),
            "env",
            "DEBIAN_FRONTEND=noninteractive",
            "apt-get",
            "install",
            "-y",
            "--no-install-recommends",
            *[f"/{DEB_STAGING}/{deb.name}" for deb in ctx.debs],
            check=False,
        )
    finally:
        runner.run("umount", str(staging), check=False)
        runner.run("rmdir", str(staging), check=False)

    if not result.ok:
        raise BuildError(
            "installing the kernel package failed inside the target.\n\n"
            "The existing bootloader is untouched and the ESP was just formatted,\n"
            "so the machine's bootability is now down to what this step did: see\n"
            "the output above before rebooting it."
        )


def _mount_chroot_fs(ctx: Context) -> None:
    """Mount /dev, /proc, /sys and /run so apt has what it needs.

    dpkg's maintainer scripts want /proc and /dev, and the postinst runs
    ``depmod``. Note what is *not* mounted for: nothing reads /sys to decide
    which drivers this machine needs, because nothing here builds a UKI.
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

    The failure this catches is a UKI that installed but was placed somewhere
    the firmware will never look.
    """
    console = ctx.console

    versioned = sorted((ctx.esp_mount / "EFI/Linux").glob("*.efi"))
    if not versioned:
        raise BuildError(
            f"the package installed, but {ctx.esp_mount}/EFI/Linux contains no .efi\n\n"
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

    _verify_copied(ctx, uki_path)
    _verify_fallback(ctx, uki_path)
    _verify_contents(ctx, uki_path)
    _verify_kernel_installed(ctx)


def _verify_copied(ctx: Context, uki_path: Path) -> None:
    """Prove the ESP holds the UKI the package brought, byte for byte.

    Both files are on the target once the package is unpacked, so this costs a
    read rather than an extraction. It is worth it: the postinst copies tens of
    megabytes onto a FAT filesystem, and a copy that stopped early produces a
    file that looks exactly like a UKI.
    """
    source = ctx.target / PACKAGED_UKI
    if not source.is_file():
        raise BuildError(
            f"the package did not unpack a UKI to {source}.\n"
            "The postinst installs from there, so whatever is on the ESP did not\n"
            "come from this package."
        )

    expected = source.stat().st_size
    actual = uki_path.stat().st_size
    if actual != expected:
        raise BuildError(
            f"{uki_path} is {actual} bytes; the package's UKI is {expected}.\n"
            "The copy onto the ESP is truncated."
        )

    if uki_path.read_bytes() != source.read_bytes():
        raise BuildError(
            f"{uki_path} differs from the package's UKI despite matching in size.\n"
            "Refusing to hand over a boot artifact that is not the one that was built."
        )
    ctx.console.ok("the UKI on the ESP is the one the package carries")


def _verify_fallback(ctx: Context, uki_path: Path) -> None:
    """Confirm the fallback loader is the same UKI.

    This is the file that makes the machine bootable without NVRAM, and the only
    one that survives a CMOS reset — so its absence is fatal here rather than a
    warning.
    """
    fallback = ctx.esp_mount / "EFI/BOOT/BOOTX64.EFI"
    if not fallback.is_file():
        raise BuildError(
            f"the package's postinst did not write {fallback}.\n\n"
            "That file is how firmware boots this machine with no configuration, and\n"
            "— because this step formats the ESP — it is also the only boot path that\n"
            "does not depend on the NVRAM entry efibootmgr is about to create. Check\n"
            "the postinst output above, and that python3 is present in the target."
        )

    if fallback.stat().st_size != uki_path.stat().st_size:
        raise BuildError(
            f"{fallback} is {fallback.stat().st_size} bytes but the UKI is "
            f"{uki_path.stat().st_size} — the copy is truncated"
        )
    ctx.console.ok("firmware fallback written: \\EFI\\BOOT\\BOOTX64.EFI")


def _verify_contents(ctx: Context, uki_path: Path) -> None:
    """Read the finished UKI's sections back out of the file.

    The command line is the one that matters, and the trap it guards is the one
    that motivated building the UKI here in the first place: the line names the
    partition the installer was *supposed* to create. If the table and the UKI
    disagree the machine installs cleanly and panics at boot with "unable to
    find root device", and the mismatch is invisible without reading the file.

    The installer checks the disk side of that same claim in
    :func:`ubuntu_uki_iso.installer.partition._verify_root_guid`, so the two
    halves are compared against the constant rather than against each other.
    """
    if pe.read_section(uki_path, ".initrd") is None:
        raise BuildError(
            "the UKI has no .initrd section — it would panic before it could find\n"
            "its root filesystem."
        )

    embedded = pe.cmdline(uki_path)
    if embedded is None:
        ctx.console.warn("could not read .cmdline out of the UKI; skipping that check")
        return

    ctx.console.info(f"embedded cmdline: {embedded}")

    expected = boot.cmdline_installed()
    if embedded.strip() != expected:
        raise BuildError(
            f"the UKI's command line is not the one this build produces.\n"
            f"  UKI:      {embedded.strip()}\n"
            f"  expected: {expected}"
        )
    ctx.console.ok("the UKI names the partition this install created")


def _verify_kernel_installed(ctx: Context) -> None:
    """Confirm the package's modules landed.

    ``/lib/modules/<release>`` exists only if dpkg unpacked the package
    completely. It is worth checking separately from the UKI, because the two
    arrive in the same file but end up in different places — and because the
    running kernel's modules are the one thing the prebuilt UKI cannot carry
    with it.
    """
    modules = ctx.target / "lib/modules" / ctx.release
    if not modules.is_dir():
        raise BuildError(
            f"the kernel package did not install: {modules} is missing.\n"
            "The UKI would boot a kernel with no modules to load."
        )
    ctx.console.ok(f"kernel installed: /lib/modules/{ctx.release}")
