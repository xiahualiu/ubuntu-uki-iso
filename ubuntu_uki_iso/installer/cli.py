"""
The installer's command line and orchestration.

DEFAULTS TO DRY-RUN. Running it with no arguments prints exactly what it would
do and changes nothing. Writing to disk takes three separate decisions::

    --apply                    stop pretending
    --raid=auto                reuse an existing array or create one on blank disks
    typing the confirmation    only for the destructive steps

The design assumption is that the most likely way to lose 58 TB is to run this
on the wrong machine, or on this one by accident. Every default is therefore
the recoverable one.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

from ..errors import (
    BuildError,
    CommandFailed,
    ConfigError,
    NotConfirmed,
    PackagesMissing,
    Refusal,
    ToolMissing,
)
from . import accounts, config, discovery, efientry, partition, preflight, raid, target, uki
from .context import TARGET_MOUNT, Context
from .preflight import RaidMode

DESCRIPTION = "Install ubuntu-uki-iso onto this machine's storage."

EPILOG = "Installs the rootfs and boot Debian package from /payload. Defaults to dry-run."


def add_arguments(parser: argparse.ArgumentParser) -> None:
    """
    Define the standalone installer's arguments.
    """
    parser.add_argument(
        "--root-disk",
        metavar="DEV",
        help="Root disk override; default: discover one SSD around 1 TB",
    )
    parser.add_argument(
        "--data-disks",
        metavar='"D1 D2 ..."',
        help="Array member overrides; default: discover eight HDDs around 8 TB",
    )
    parser.add_argument(
        "--expected-md-devices",
        type=int,
        default=config.EXPECTED_MD_DEVICES,
        help="Expected RAID member count (default: %(default)s)",
    )
    parser.add_argument(
        "--raid",
        choices=[m.value for m in RaidMode],
        default=RaidMode.AUTO.value,
        help="auto (default): preserve an existing array or create one on blank disks; reuse, create, or none",
    )
    parser.add_argument(
        "--payload",
        metavar="PATH",
        help="Rootfs squashfs to install. Autodetected on the ISO, which also "
        "carries boot-installed.deb, uki.efi and manifest.json beside it.",
    )
    parser.add_argument(
        "--esp-size", default="1G", metavar="SIZE", help="EFI System Partition size (default 1G)"
    )

    parser.add_argument("--username", help="Installed login name; prompted when omitted")
    parser.add_argument(
        "--password-file",
        type=Path,
        help="Private password file for automation; otherwise prompt with confirmation",
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


def build_parser() -> argparse.ArgumentParser:
    """
    A standalone parser, for running the installer directly.
    """
    parser = argparse.ArgumentParser(
        prog="ubuntu-uki-install",
        description=DESCRIPTION,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=EPILOG,
    )
    add_arguments(parser)
    return parser


def _resolve_mode(args: argparse.Namespace) -> bool:
    """
    Return whether to run dry. Exits with a message on a contradictory ask.
    """
    if args.apply and args.dry_run:
        raise Refusal("--apply and --dry-run are mutually exclusive")
    if args.assume_yes and not args.apply:
        raise Refusal("--assume-yes only means something with --apply")
    return not args.apply


def _print_plan(ctx: Context, preflight_result: preflight.Preflight) -> None:
    print("==>", "plan")

    match preflight_result.decision:
        case preflight.Decision.REUSE:
            print(f"RAID: preserve the existing {config.EXPECTED_MD_LEVEL} array")
            print(
                f"      uuid {config.EXPECTED_MD_UUID}, "
                f"{len(ctx.data_disks)} disks, {config.EXPECTED_MD_CHUNK} chunks"
            )
            print("      no superblock is written on this path")
        case preflight.Decision.CREATE:
            print(
                f"RAID: CREATE a new {config.EXPECTED_MD_LEVEL} array across "
                f"{len(ctx.data_disks)} disks"
            )
            print("      all target disks verified blank")
        case _:
            print("RAID: not managed (--raid=none)")

    if ctx.data_disks:
        print(f"data disks: {', '.join(ctx.data_disks)}")
    print(f"root: partition {ctx.root_disk} (GPT: ESP {ctx.esp_size} + root)")
    print("      format the root filesystem ext4")
    print(f"      write the payload onto it: {ctx.payload}")
    kernel = ctx.release or "<release>"
    print(f"      install the boot package ({kernel}) in the target chroot")
    print("boot: create a firmware entry and EFI fallback loader")


def _cleanup(ctx: Context) -> None:
    """
    Unmount everything this run mounted.

    More important than it looks: the target is mounted beneath a running live
    system, so leaving it mounted after a failure means the next run formats a
    filesystem that is still in use.
    """
    if ctx.dry_run:
        return
    for path in (ctx.esp_mount, TARGET_MOUNT):
        if path.exists():
            subprocess.run(["umount", "-R", str(path)], check=False)


def install(args: argparse.Namespace) -> int:
    # Annotated rather than inferred: argparse hands back `Any`, and pyright
    # models `Any or ""` as `LiteralString | Any`, whose `.split()` is a
    # `list[LiteralString]` — not assignable to the `list[str]` downstream.
    raw_data_disks: str = args.data_disks or ""
    data_disks = raw_data_disks.split()
    mode = RaidMode.parse(args.raid)

    dry_run = _resolve_mode(args)

    print("ubuntu-uki-install")
    if dry_run:
        print("DRY RUN — nothing will be written. Add --apply to execute.")
    else:
        print("APPLYING — this WILL write to disk.")
        if args.assume_yes:
            print(
                "WARN:", "--assume-yes is set: the typed confirmation is skipped", file=sys.stderr
            )

    if not dry_run:
        import os

        if os.geteuid() != 0:
            raise Refusal("installation with --apply requires root")
        if not Path("/sys/firmware/efi").is_dir():
            raise Refusal("installation requires booting the LiveUSB via UEFI")
    tools = (
        "blkid",
        "blockdev",
        "findmnt",
        "lsblk",
        "mdadm",
        "sgdisk",
        "mkfs.ext4",
        "mkfs.vfat",
        "mount",
        "umount",
        "unsquashfs",
        "efibootmgr",
        "udevadm",
        "chroot",
        "dpkg-deb",
    )

    missing = [tool for tool in tools if shutil.which(tool) is None]
    if missing:
        raise ToolMissing(*missing)

    root_disk, data_disks = discovery.select(
        args.root_disk,
        data_disks,
        manage_data=mode is not RaidMode.NONE,
        expected_devices=args.expected_md_devices,
    )

    ctx = Context(
        root_disk=root_disk,
        data_disks=data_disks,
        dry_run=dry_run,
        mode=mode,
        esp_size=args.esp_size,
        md_device=config.MD_DEVICE,
        force_root_disk=args.force_root_disk,
        payload=Path(args.payload) if args.payload else None,
    )

    # Read-only, and before the plan, so the plan names a real file and a real
    # kernel rather than a placeholder.
    target.find_payload(ctx)

    result = preflight.scan(data_disks or [])
    preflight.report(result, root_disk)
    preflight.decide(
        result,
        root_disk,
        mode,
        expected_devices=getattr(args, "expected_md_devices", config.EXPECTED_MD_DEVICES),
    )
    preflight.enforce(result)
    preflight.check_root_disk(root_disk, force=args.force_root_disk)

    _print_plan(ctx, result)

    if dry_run:
        print("")
        print("Nothing was changed.")
        print("Re-run with --apply to execute the plan above.")
        return 0

    if not args.assume_yes:
        _confirm(ctx, result)

    credentials = accounts.collect(ctx, args.username, args.password_file)
    ctx.confirmed = True

    try:
        partition.run(ctx)
        raid.run(ctx, result)
        target.run(ctx)
        accounts.create(ctx, credentials)
        uki.run(ctx)
        efientry.run(ctx)
    finally:
        credentials.password = ""
        _cleanup(ctx)

    print("==>", "done")
    print(f"root filesystem UUID: {ctx.root_uuid}")
    print(f"kernel: {ctx.release}")
    print("")
    print("The firmware entry for the new UKI has been created, and")
    print("\\EFI\\BOOT\\BOOTX64.EFI boots the same kernel with no configuration.")
    print("The pre-existing bootloader entry was left alone.")
    return 0


def _confirm(ctx: Context, result: preflight.Preflight) -> None:
    """
    One confirmation, with the consequence spelled out.

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
    print("WARN:", "\n".join(lines), file=sys.stderr)
    phrase = f"destroy {ctx.root_disk}"
    try:
        with open("/dev/tty", "r+", encoding="utf-8") as tty:
            tty.write(f"Type {phrase} to proceed: ")
            tty.flush()
            answer = tty.readline().strip()
    except OSError as exc:
        raise NotConfirmed("no terminal available; use --assume-yes only for automation") from exc
    if answer != phrase:
        raise NotConfirmed("confirmation phrase did not match — nothing was done")
    ctx.confirmed = True


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    args = build_parser().parse_args(argv)

    try:
        return install(args)
    except Refusal as exc:
        print("REFUSING", exc.reason, file=sys.stderr)
        return Refusal.exit_code
    except (
        BuildError,
        CommandFailed,
        ConfigError,
        NotConfirmed,
        PackagesMissing,
        ToolMissing,
        OSError,
        subprocess.SubprocessError,
    ) as exc:
        print("ERROR:", str(exc), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("ERROR:", "interrupted — nothing further was done", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
