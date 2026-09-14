"""GPT on the root disk: an EFI System Partition and the root filesystem.

Two partitions, because the design has two. The ESP is sized for roughly ten
to fifteen UKIs at about 50 MB each, which is why retention is deliberate
rather than optional.

The data array is not partitioned here or ever. Its members are whole disks
with md superblocks and no partition table — matching that is part of what
makes the install reproducible.
"""

from __future__ import annotations

from .. import settings
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
        "--change-name=2=ubuntu-uki-iso-root",
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
        console.ok("partitions created")
