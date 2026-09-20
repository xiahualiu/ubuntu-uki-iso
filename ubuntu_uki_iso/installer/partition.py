"""GPT on the root disk: an EFI System Partition and the root filesystem.

Two partitions, because the design has two. The ESP is sized for roughly ten
to fifteen UKIs at about 50 MB each, which is why retention is deliberate
rather than optional.

The data array is not partitioned here or ever. Its members are whole disks
with md superblocks and no partition table — matching that is part of what
makes the install reproducible.
"""

from __future__ import annotations

from pathlib import Path

from .. import gpt, settings
from ..errors import BuildError
from . import device
from .context import Context


def run(ctx: Context) -> None:
    console, runner = ctx.console, ctx.runner
    ctx.derive_partitions()

    console.step("1/5 partition")
    console.info(f"{ctx.root_disk}: GPT")
    console.info(f"  {ctx.part_esp}  EFI System Partition, {ctx.esp_size}")
    console.info(f"  {ctx.part_root}  {settings.ROOT_LABEL}, remaining space")

    # --zap-all removes both the GPT and the MBR structures. Without the MBR
    # wipe, a disk that previously had a hybrid layout keeps a stale protective
    # MBR that some firmware will still offer as a boot target.
    runner.destructive(
        f"erase the partition table on {ctx.root_disk}",
        "sgdisk",
        "--zap-all",
        ctx.root_disk,
    )

    runner.run(
        "sgdisk",
        "--new=1:0:+" + ctx.esp_size,
        "--typecode=1:ef00",
        "--change-name=1:ESP",
        ctx.root_disk,
    )
    runner.run(
        "sgdisk",
        "--new=2:0:0",
        "--typecode=2:8300",
        # Fixed, not generated. This GUID is what the command line inside the
        # prebuilt UKI names, so it is the one thing about this partition that
        # is decided before the disk is touched — and it has to be written
        # exactly as settings.ROOT_PARTUUID spells it, lowercase, because that
        # is the form the kernel matches and /dev/disk/by-partuuid uses.
        f"--partition-guid=2:{settings.ROOT_PARTUUID}",
        f"--change-name=2={settings.ROOT_LABEL}",
        ctx.root_disk,
    )

    # Make the kernel re-read the table, then wait for the nodes to actually
    # appear — mkfs against a partition node that does not exist yet is a race
    # that reproduces about one run in ten.
    if runner.have("partx"):
        runner.run("partx", "-u", ctx.root_disk, check=False)
    else:
        runner.run("blockdev", "--rereadpt", ctx.root_disk, check=False)
    device.settle_udev(runner)

    if not ctx.dry_run:
        for path in (ctx.part_esp, ctx.part_root):
            if not device.exists(path):
                raise BuildError(f"{path} did not appear after partitioning")
        _verify_root_guid(ctx)
        console.ok("partitions created")


def _verify_root_guid(ctx: Context) -> None:
    """Read the table back, rather than trusting sgdisk's exit code.

    The root partition's GUID is the one value the prebuilt UKI's command line
    depends on, and it is also the only one sgdisk was *told* rather than asked
    for — so it is the one that can silently come out wrong. A mismatch here is
    a machine that installs perfectly and panics at first boot with "unable to
    find root device", which is the failure this check exists to move from
    after the reboot to during the install.
    """
    table = gpt.parse(Path(ctx.root_disk))
    root = table.by_number(2)
    if root is None:
        raise BuildError(
            f"{ctx.root_disk} has no second partition after partitioning.\n"
            f"Partitions found: {len(table.partitions)}"
        )

    expected = settings.ROOT_PARTUUID.lower()
    if root.partuuid != expected:
        raise BuildError(
            f"{ctx.root_disk} partition 2 has PARTUUID {root.partuuid},\n"
            f"but the UKI's command line names {expected}.\n"
            "The machine would install and then panic at first boot looking for a\n"
            "partition that does not exist. The disk has been repartitioned but\n"
            "nothing has been written to the filesystems yet."
        )
    ctx.console.ok(f"root partition PARTUUID: {root.partuuid}")
