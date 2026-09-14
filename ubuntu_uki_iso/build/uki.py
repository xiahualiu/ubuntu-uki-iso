"""Build the ISO's own UKI.

The UKI is produced by running ``kernel-install`` *inside the live rootfs*, not
by calling ukify directly with flags assembled here. That matters: with
``layout=uki``, kernel-install is what wires dracut to ukify to
``90-uki-copy.install``, and the command line comes from
``/etc/kernel/cmdline`` on disk. So the ISO's boot parameters are system state
that can be inspected and diffed, rather than a string that exists only in a
build script.

The command line is the one difference between this UKI and the installed
system's. They share the kernel build and the generation path; the live image
must point at the squashfs, the installed image must point at its root
filesystem. Everything else is identical.
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

from .. import pe
from ..errors import BuildError
from ..log import Console, get_console
from ..paths import Layout
from ..proc import Runner

#: Inside the rootfs, so kernel-install sees it as a path in the guest.
STAGING_REL = Path("var/tmp/esp-staging")


def build(layout: Layout, console: Console, runner: Runner) -> Path:
    rootfs = layout.rootfs_live
    if not rootfs.is_dir():
        raise BuildError(f"no live rootfs at {rootfs}. Run `ubuntu-uki-iso build rootfs` first.")

    release = layout.kernel_release()
    kernel = rootfs / f"boot/vmlinuz-{release}"
    if not kernel.is_file():
        raise BuildError(f"{kernel} is missing — the rootfs was built without the custom kernel")

    layout.uki.mkdir(parents=True, exist_ok=True)
    for stale in layout.uki.glob("*.efi"):
        stale.unlink()

    staging = rootfs / STAGING_REL
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True)

    mounted = _mount_chroot_fs(rootfs, runner)
    try:
        console.step(f"uki: kernel-install {release} (dracut + ukify)")
        console.info(f"cmdline: {(rootfs / 'etc/kernel/cmdline').read_text().strip()}")

        # Without a real ESP, 90-uki-copy.install has nowhere to put the UKI.
        # KERNEL_INSTALL_BOOT_ROOT points the whole layout at a scratch
        # directory inside the rootfs, which is harvested below.
        runner.run(
            "chroot",
            str(rootfs),
            "env",
            f"KERNEL_INSTALL_BOOT_ROOT=/{STAGING_REL}",
            "kernel-install",
            "add",
            release,
            f"/boot/vmlinuz-{release}",
            check=True,
        )
    finally:
        _umount_chroot_fs(rootfs, mounted, runner)

    produced = sorted(staging.glob("*.efi"))
    if not produced:
        raise BuildError(f"kernel-install reported success but produced no UKI under {staging}")

    source = produced[0]
    shutil.copyfile(source, layout.uki_file)
    console.grey(f"kernel-install produced: {source.name}")

    _verify(layout, console)
    console.info(f"UKI: {layout.uki_file} ({layout.uki_file.stat().st_size // 1024 // 1024} MB)")
    return layout.uki_file


def _mount_chroot_fs(rootfs: Path, runner: Runner) -> list[Path]:
    """Mount /dev, /proc and /sys so kernel-install has what it needs."""
    mounted = []
    for source, target, fstype in (
        ("/dev", rootfs / "dev", None),
        ("proc", rootfs / "proc", "proc"),
        ("sys", rootfs / "sys", "sysfs"),
    ):
        target.mkdir(parents=True, exist_ok=True)
        argv = ["mount"]
        argv += ["-t", fstype] if fstype else ["--bind"]
        argv += [source, str(target)]
        result = runner.run(*argv, check=False)
        if result.ok:
            mounted.append(target)
    return mounted


def _umount_chroot_fs(rootfs: Path, mounted: list[Path], runner: Runner) -> None:
    """Unmount in reverse. Lazy, so a busy mount cannot wedge the build."""
    for target in reversed(mounted):
        runner.run("umount", "-l", str(target), check=False)


def _verify(layout: Layout, console: Console) -> None:
    """Check the sections, rather than trusting the pipeline.

    A UKI missing its initramfs or its command line boots to a firmware error
    or an empty kernel panic. Both are cheap to rule out here and expensive to
    discover on the target.
    """
    console.step("uki: verify")

    embedded = pe.cmdline(layout.uki_file)
    if embedded is None:
        console.warn(
            "the UKI has no .cmdline section; it would rely on the firmware's "
            "command line, which this design does not provide"
        )
        return

    console.info(f".cmdline: {embedded}")
    if "root=live:" not in embedded:
        raise BuildError(
            "the UKI's command line does not contain root=live: — it would not "
            f"boot the ISO.\nIt says: {embedded}"
        )

    if pe.read_section(layout.uki_file, ".initrd") is None:
        raise BuildError("the UKI has no .initrd section — dracut did not contribute one")

    console.ok("command line points at the live squashfs, initramfs present")


def main(argv: list[str] | None = None) -> int:
    console = get_console()
    layout = Layout()
    runner = Runner(dry_run=False, console=console)
    runner.require("chroot", "mount", "umount", "kernel-install")
    build(layout, console, runner)
    console.step("uki: done")
    return 0


if __name__ == "__main__":
    sys.exit(main())
