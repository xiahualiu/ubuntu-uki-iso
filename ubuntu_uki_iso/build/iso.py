"""Assemble the UEFI-bootable hybrid ISO with xorriso.

Layout produced::

    /EFI/BOOT/BOOTX64.EFI     inside efi.img — the UKI, booted by firmware
    /boot/efi.img             the FAT image, attached as an El Torito entry
                              and appended as a GPT EFI System Partition
    /live/filesystem.squashfs the live environment
    /payload/rootfs.squashfs  the system the installer writes to disk
    /payload/debs/*.deb       the UKI package — kernel, modules and boot artifact

No BIOS boot, no isolinux, no El Torito emulation. Every target is UEFI.

Two independent ways this image can fail to boot, and they fail differently:

*CD/DVD* needs the El Torito EFI entry.
*USB* needs an MBR and a GPT carrying a partition typed as an EFI System
Partition — firmware writing the image to a stick does not look at El Torito at
all.

An image with the first and not the second builds cleanly, boots in a VM with
``-cdrom``, and is a coaster on a USB stick. Both are therefore checked, and
both are fatal.

The payload is checked the same way and for the same reason. An ISO that boots
and then has nothing to install fails on the target, after partitioning, with
the disk already formatted — so what the medium must carry is gated here.
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

from .. import gpt, paths, settings
from ..config import host_packages
from ..errors import BuildError
from ..log import Console, get_console
from ..paths import Layout
from ..proc import Runner

#: FAT volume labels are capped at 11 characters and mkfs.vfat fails outright
#: rather than truncating, so an over-long name breaks the build at the last
#: step. Checked rather than trusted.
FAT_LABEL_MAX = 11

#: FAT32 needs at least ~33 MB; round up generously for filesystem metadata.
EFI_IMAGE_MIN_MB = 40
EFI_IMAGE_HEADROOM_MB = 16


def _build_efi_image(layout: Layout, console: Console, runner: Runner) -> Path:
    """The FAT image holding the UKI, built without a loop device or a mount.

    ``mkfs.vfat`` writes to a plain file and mtools populates it, so this needs
    no privileges. That is what lets the whole ISO build run inside a container
    that has no access to the host's block devices.
    """
    uki = layout.uki_file
    if not uki.is_file():
        raise BuildError(f"no UKI at {uki}. Run `ubuntu-uki-iso build uki` first.")

    label = settings.ESP_LABEL
    if len(label) > FAT_LABEL_MAX:
        raise BuildError(
            f"ESP_LABEL is {len(label)} characters ({label!r}); FAT allows at most {FAT_LABEL_MAX}"
        )

    size_mb = max(
        EFI_IMAGE_MIN_MB,
        uki.stat().st_size // (1024 * 1024) + EFI_IMAGE_HEADROOM_MB,
    )

    image = layout.iso_stage / "boot/efi.img"
    image.parent.mkdir(parents=True, exist_ok=True)
    image.unlink(missing_ok=True)

    console.step("iso: build efi.img")
    runner.run(
        "dd", "if=/dev/zero", f"of={image}", "bs=1M", f"count={size_mb}", "status=none", check=True
    )
    runner.run("mkfs.vfat", "-F", "32", "-n", label, str(image), check=True, quiet=True)
    runner.run("mmd", "-i", str(image), "::/EFI", "::/EFI/BOOT", check=True)
    runner.run("mcopy", "-i", str(image), str(uki), "::/EFI/BOOT/BOOTX64.EFI", check=True)

    console.info(f"efi.img: {size_mb} MB, UKI {uki.stat().st_size // 1024 // 1024} MB")
    return image


def _run_xorriso(layout: Layout, efi_image: Path, console: Console, runner: Runner) -> None:
    """Burn the ISO.

    On ``-isohybrid-gpt-basdat``: the design notes name it and it is kept for
    continuity, but testing against xorriso 1.5.6 shows it is *not* what makes
    this image hybrid. On its own it produces no MBR and no GPT at all. What
    creates the ESP is ``-append_partition`` plus ``-appended_part_as_gpt``.

    Note the two different paths to the same file, which are not
    interchangeable: ``-e`` takes a path *inside* the ISO filesystem, and
    ``-append_partition`` takes one on the *build host*. Getting them the wrong
    way round fails at the very last step with "Cannot open data file for
    appended partition".
    """
    argv = [
        "xorriso",
        "-as",
        "mkisofs",
        "-volid",
        settings.VOLID,
        "-iso-level",
        "3",
        "-full-iso9660-filenames",
        "-joliet",
        "-joliet-long",
        "-rational-rock",
        "-e",
        "boot/efi.img",
        "-no-emul-boot",
        "-append_partition",
        "2",
        "0xef",
        str(efi_image),
        "-appended_part_as_gpt",
        "-isohybrid-gpt-basdat",
    ]

    # Pin the ISO's own timestamps so two builds of the same staging tree are
    # comparable. Best effort: see the reproducibility note in the README.
    if epoch := os.environ.get("SOURCE_DATE_EPOCH"):
        argv += ["-volume_date", f"all_file_dates={epoch}"]

    argv += ["-o", str(layout.iso), str(layout.iso_stage)]

    console.step("iso: xorriso")
    layout.iso.unlink(missing_ok=True)
    runner.run(*argv, check=True)

    if not layout.iso.is_file():
        raise BuildError("xorriso produced no ISO")


def _verify(layout: Layout, console: Console, runner: Runner) -> None:
    console.step("iso: verify")

    _verify_volume_label(layout, console, runner)
    _verify_el_torito(layout, console, runner)
    _verify_gpt(layout, console)


def _verify_volume_label(layout: Layout, console: Console, runner: Runner) -> None:
    """The label is load-bearing: the live cmdline searches for it by name.

    If the label burned into the ISO differs from the one dracut looks for, the
    initramfs finds no medium and drops to a rescue shell with no explanation.
    """
    report = runner.capture("xorriso", "-indev", str(layout.iso), "-pvd_info", check=False)
    label = ""
    for line in report.splitlines():
        if line.strip().startswith("Volume id"):
            label = line.split(":", 1)[1].strip()
            break

    if not label:
        console.warn("could not read the ISO's volume id; skipping that check")
        return

    if label != settings.VOLID:
        raise BuildError(
            f"the ISO's volume id is {label!r} but the live command line expects "
            f"{settings.VOLID!r}.\nThe ISO would boot to a rescue shell looking for "
            "a medium that is not there."
        )
    console.info(f"volume id: {label}  (matches root=live:CDLABEL=)")


def _verify_el_torito(layout: Layout, console: Console, runner: Runner) -> None:
    report = runner.capture(
        "xorriso", "-indev", str(layout.iso), "-report_el_torito", "plain", check=False
    )
    if gpt.parse_el_torito(report):
        console.info("El Torito: UEFI boot entry present (CD/DVD)")
        return

    raise BuildError(
        "no bootable UEFI El Torito entry — this ISO will not boot from a CD/DVD.\n\n"
        f"xorriso reported:\n{report}"
    )


def _verify_gpt(layout: Layout, console: Console) -> None:
    """The check that would have caught the design's original flag set."""
    table = gpt.parse(layout.iso)

    if not table.has_mbr:
        raise BuildError(
            "the ISO has no MBR, so it will not boot from a USB stick.\n"
            "It would still boot from a CD/DVD via El Torito."
        )
    if not table.has_gpt:
        raise BuildError(
            "the ISO has an MBR but no GPT, so firmware will not find an EFI "
            "System Partition on a USB stick."
        )

    esp = table.esp()
    if esp is None:
        listed = (
            "\n".join(
                f"  {partition.type_guid}  {partition.name!r}" for partition in table.partitions
            )
            or "  (no partitions)"
        )
        raise BuildError(
            "the ISO's GPT has no partition typed as an EFI System Partition "
            f"({gpt.ESP_TYPE_GUID}), so firmware will not find the UKI.\n"
            f"Partitions found:\n{listed}\n"
            "Check that -append_partition uses type code 0xef."
        )

    console.info(f"GPT: EFI System Partition present, {esp.size_mb} MB (USB)")


def _stage_uki_package(layout: Layout, console: Console) -> None:
    """Put the UKI package on the medium, beside the payload.

    This is the whole of what the target installs: the kernel, its modules and
    the UKI, in one package. It sits next to the payload because everything the
    installer consumes from the medium lives in one directory — and because a
    later kernel update on the installed machine installs a newer version of
    this same file, so the medium and the update path carry the same artifact.
    """
    package = layout.uki_package
    if not package.is_file():
        raise BuildError(
            f"no UKI package at {package}.\n"
            "Run `ubuntu-uki-iso build uki-target` first. Without it the ISO boots,\n"
            "installs a root filesystem, and leaves a machine with no kernel."
        )

    destination = layout.iso_stage / paths.PAYLOAD_DEBS
    destination.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(package, destination / package.name)
    console.info(f"payload: {paths.PAYLOAD_DEBS}/{package.name}")


def build(layout: Layout, console: Console, runner: Runner) -> Path:
    """Gate what the medium must carry, then burn it.

    The gates are on the *staging tree*, which is what xorriso is handed: a file
    missing from it is a file missing from the ISO. What they exist to catch is
    the failure that otherwise waits for the target — an ISO that boots and then
    has nothing to install onto the machine.
    """
    if not (layout.iso_stage / paths.LIVE_SQUASHFS).is_file():
        raise BuildError(
            f"no live squashfs under {layout.iso_stage}. Run `ubuntu-uki-iso build squashfs` first."
        )
    if not (layout.iso_stage / paths.PAYLOAD_SQUASHFS).is_file():
        raise BuildError(
            f"no installation payload at {layout.iso_stage / paths.PAYLOAD_SQUASHFS}.\n"
            "Without it the ISO boots and then has no root filesystem to write to\n"
            "the target. Run `ubuntu-uki-iso build squashfs` first."
        )
    if not layout.uki_file.is_file():
        raise BuildError(f"no UKI at {layout.uki_file}. Run `ubuntu-uki-iso build uki` first.")

    _stage_uki_package(layout, console)

    _run_xorriso(layout, _build_efi_image(layout, console, runner), console, runner)
    _verify(layout, console, runner)

    console.step("iso: done")
    size_mb = layout.iso.stat().st_size // 1024 // 1024
    console.info(f"{size_mb} MB  {layout.iso}")
    return layout.iso


def main(argv: list[str] | None = None) -> int:
    console = get_console()
    layout = Layout()
    runner = Runner(dry_run=False, console=console)
    runner.require("xorriso", "mkfs.vfat", "mmd", "mcopy", "dd")
    runner.require_packages(*host_packages("build"))
    build(layout, console, runner)
    return 0


if __name__ == "__main__":
    sys.exit(main())
