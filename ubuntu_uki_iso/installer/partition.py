"""Partition the target root disk into an ESP and a labeled ext4 root."""

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
        # Keep partition GUIDs reproducible; boot selection uses the GPT name below.
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
        _verify_root_partition(ctx)
        console.ok("partitions created")


def _verify_root_partition(ctx: Context) -> None:
    """
    Verify that the GPT root partition label matches the prebuilt UKI command line.

    Args:
        ctx: Installation context identifying the partitioned root disk.

    Returns:
        None. Raises BuildError if the root partition or its expected label is missing.
    """
    table = gpt.parse(Path(ctx.root_disk))
    root = table.by_number(2)
    if root is None:
        raise BuildError(
            f"{ctx.root_disk} has no second partition after partitioning.\n"
            f"Partitions found: {len(table.partitions)}"
        )

    expected = settings.ROOT_LABEL
    if root.name != expected:
        raise BuildError(
            f"{ctx.root_disk} partition 2 has PARTLABEL {root.name!r},\n"
            f"but the UKI's command line names {expected!r}.\n"
            "The disk has been repartitioned but nothing has been written to the filesystems yet."
        )
    ctx.console.ok(f"root partition PARTLABEL: {root.name}")
