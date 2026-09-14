"""The installer's command line and orchestration.

DEFAULTS TO DRY-RUN. Running it with no arguments prints exactly what it would
do and changes nothing. Writing to disk takes three separate decisions::

    --apply                    stop pretending
    --raid=create              only if a new array is actually wanted
    typing the confirmation    only for the destructive steps

The design assumption is that the most likely way to lose 58 TB is to run this
on the wrong machine, or on this one by accident. Every default is therefore
the recoverable one.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .. import settings
from ..errors import Refusal, XiahualabError
from ..log import Console, get_console
from ..paths import Layout
from ..proc import Runner
from . import efientry, partition, preflight, raid, target, uki
from .context import TARGET_MOUNT, Context
from .preflight import RaidMode

DESCRIPTION = "Install ubuntu-uki-iso onto this machine's storage."

EPILOG = (
    "WHAT IT DOES NOT DO\n"
    "  It does not create swap. The installed system has none: no swapfile,\n"
    "  no swap partition, no zram.\n"
    "\n"
    "  It does not overwrite a pre-existing bootloader entry. The firmware entry\n"
    "  it creates points at \\EFI\\BOOT\\BOOTX64.EFI, which the kernel-install\n"
    "  plugin keeps pointed at the current kernel.\n"
)


def add_arguments(parser: argparse.ArgumentParser) -> None:
    """Add the installer's arguments to an existing parser.

    Arguments rather than a whole parser, so the subcommand can be grafted into
    the main CLI without reaching into argparse's internals — which, it turns
    out, do not exist for this.
    """
    parser.add_argument(
        "--root-disk",
        metavar="DEV",
        help="Disk to partition for the system (the NVMe), e.g. /dev/nvme0n1",
    )
    parser.add_argument(
        "--data-disks", metavar='"D1 D2 ..."', help="The eight array members, space separated"
    )
    parser.add_argument(
        "--raid",
        choices=[m.value for m in RaidMode],
        default=RaidMode.REUSE.value,
        help="reuse the existing array (default), create a new one, or none",
    )
    parser.add_argument(
        "--payload",
        metavar="PATH",
        help="Rootfs squashfs to install. Autodetected on the ISO, which also "
        "carries the kernel packages in a debs/ directory beside it.",
    )
    parser.add_argument(
        "--esp-size", default="1G", metavar="SIZE", help="EFI System Partition size (default 1G)"
    )

    mode = parser.add_argument_group("mode")
    mode.add_argument(
        "--dry-run",
        action="store_true",
        default=None,
        help="print the plan and change nothing. THIS IS THE DEFAULT.",
    )
    mode.add_argument(
        "--apply", action="store_true", help="actually do it. Required for anything to be written."
    )
    mode.add_argument(
        "--assume-yes",
        action="store_true",
        help="skip the typed confirmation. Requires --apply. Automation only.",
    )

    parser.add_argument(
        "--force-root-disk",
        action="store_true",
        help="permit a rotational root disk. Read the error first.",
    )
    parser.add_argument(
        "--bless",
        action="store_true",
        help="Repoint \\EFI\\BOOT\\BOOTX64.EFI at the running kernel's UKI.",
    )


def build_parser() -> argparse.ArgumentParser:
    """A standalone parser, for running the installer directly."""
    parser = argparse.ArgumentParser(
        prog="ubuntu-uki-iso install",
        description=DESCRIPTION,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=EPILOG,
    )
    add_arguments(parser)
    return parser


def _resolve_mode(args: argparse.Namespace, console: Console) -> bool:
    """Return whether to run dry. Exits with a message on a contradictory ask."""
    if args.apply and args.dry_run:
        raise Refusal("--apply and --dry-run are mutually exclusive")
    if args.assume_yes and not args.apply:
        raise Refusal("--assume-yes only means something with --apply")
    return not args.apply


def _print_plan(ctx: Context, preflight_result: preflight.Preflight) -> None:
    console = ctx.console
    console.step("plan")

    match preflight_result.decision:
        case preflight.Decision.REUSE:
            console.info(f"RAID: assemble the existing {settings.EXPECTED_MD_LEVEL} array")
            console.info(
                f"      uuid {settings.EXPECTED_MD_UUID}, "
                f"{settings.EXPECTED_MD_DEVICES} disks, {settings.EXPECTED_MD_CHUNK} chunks"
            )
            console.info("      no superblock is written on this path")
        case preflight.Decision.CREATE:
            console.info(
                f"RAID: CREATE a new {settings.EXPECTED_MD_LEVEL} array across "
                f"{len(ctx.data_disks)} disks"
            )
            console.info("      all target disks verified blank")
        case _:
            console.info("RAID: not managed (--raid=none)")

    console.info(f"root: partition {ctx.root_disk} (GPT: ESP {ctx.esp_size} + root)")
    console.info("      format the root filesystem ext4")
    console.info(f"      write the payload onto it: {ctx.payload}")
    kernel = ctx.release or "<release>"
    console.info(f"      install the kernel package ({kernel}) — it builds its own UKI")
    console.info("boot: write \\EFI\\BOOT\\BOOTX64.EFI and create a firmware entry")


def _cleanup(ctx: Context) -> None:
    """Unmount everything this run mounted.

    More important than it looks: the target is mounted beneath a running live
    system, so leaving it mounted after a failure means the next run formats a
    filesystem that is still in use.
    """
    runner = ctx.runner
    if ctx.dry_run:
        return
    for path in (ctx.esp_mount, TARGET_MOUNT):
        if path.exists():
            runner.run("umount", "-R", str(path), check=False)


def install(args: argparse.Namespace, console: Console) -> int:
    if not args.root_disk:
        raise Refusal("--root-disk is required")

    # Annotated rather than inferred: argparse hands back `Any`, and pyright
    # models `Any or ""` as `LiteralString | Any`, whose `.split()` is a
    # `list[LiteralString]` — not assignable to the `list[str]` downstream.
    raw_data_disks: str = args.data_disks or ""
    data_disks = raw_data_disks.split()
    mode = RaidMode.parse(args.raid)
    if mode is not RaidMode.NONE and not data_disks:
        raise Refusal("--data-disks is required (or pass --raid=none)")

    dry_run = _resolve_mode(args, console)

    console.info("ubuntu-uki-iso install")
    if dry_run:
        console.info(
            f"{console.bold('DRY RUN')} — nothing will be written. Add --apply to execute."
        )
    else:
        console.info(f"{console.red('APPLYING')} — this WILL write to disk.")
        if args.assume_yes:
            console.warn("--assume-yes is set: the typed confirmation is skipped")

    runner = Runner(dry_run=dry_run, console=console)
    runner.require(
        "blkid",
        "findmnt",
        "lsblk",
        "mdadm",
        "sgdisk",
        "mkfs.ext4",
        "mkfs.vfat",
        "mount",
        "umount",
        "unsquashfs",
        "chroot",
        "dpkg-deb",
    )

    layout = Layout()
    ctx = Context(
        root_disk=args.root_disk,
        data_disks=data_disks,
        runner=runner,
        console=console,
        layout=layout,
        mode=mode,
        esp_size=args.esp_size,
        md_device=settings.MD_DEVICE,
        force_root_disk=args.force_root_disk,
        payload=Path(args.payload) if args.payload else None,
    )

    # Read-only, and before the plan, so the plan names a real file and a real
    # kernel rather than a placeholder.
    target.find_payload(ctx)
    ctx.load_release()

    result = preflight.scan(runner, data_disks or [])
    preflight.report(result, runner, args.root_disk, console)
    preflight.decide(result, args.root_disk, mode)
    preflight.enforce(result, console)
    preflight.check_root_disk(runner, args.root_disk, force=args.force_root_disk)

    _print_plan(ctx, result)

    if dry_run:
        console.info("")
        console.info(f"{console.green('Nothing was changed.')}")
        console.info("Re-run with --apply to execute the plan above.")
        return 0

    if not args.assume_yes:
        _confirm(ctx, result, runner)

    try:
        partition.run(ctx)
        raid.run(ctx, result)
        target.run(ctx)
        uki.run(ctx)
        efientry.run(ctx)
    finally:
        _cleanup(ctx)

    console.step("done")
    console.info(f"root filesystem UUID: {ctx.root_uuid}")
    console.info(f"kernel: {ctx.release}")
    console.info("")
    console.info("The firmware entry for the new UKI has been created, and")
    console.info("\\EFI\\BOOT\\BOOTX64.EFI boots the same kernel with no configuration.")
    console.info("The pre-existing bootloader entry was left alone.")
    return 0


def _confirm(ctx: Context, result: preflight.Preflight, runner: Runner) -> None:
    """One confirmation, with the consequence spelled out.

    Once, here, rather than each destructive command asking for itself —
    which trains the operator to type the phrase without reading it.
    """
    lines = [
        f"About to repartition {ctx.root_disk}.",
        f"Everything currently on {ctx.root_disk} will be gone.",
    ]
    if result.decision is preflight.Decision.CREATE:
        lines += [
            f"A NEW RAID0 array will be created across {len(ctx.data_disks)} disks:",
            f"  {' '.join(ctx.data_disks)}",
            "Every byte on those disks will be gone.",
        ]
    runner.confirm(f"destroy {ctx.root_disk}", "\n".join(lines))


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    console = get_console()
    args = build_parser().parse_args(argv)

    try:
        if args.bless:
            runner = Runner(dry_run=_resolve_mode(args, console), console=console)
            efientry.bless(runner, console)
            return 0
        return install(args, console)
    except Refusal as exc:
        console.refuse(exc.reason)
        return Refusal.exit_code
    except XiahualabError as exc:
        console.error(str(exc))
        return 1
    except KeyboardInterrupt:
        console.error("interrupted — nothing further was done")
        return 130


if __name__ == "__main__":
    sys.exit(main())
