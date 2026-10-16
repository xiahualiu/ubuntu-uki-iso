"""
Partition the target root disk into an ESP and a labeled ext4 root.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from .. import gpt, settings
from ..errors import BuildError
from . import config, device
from .context import Context


def run(ctx: Context) -> None:
    if ctx.dry_run:
        print("dry run: this step would modify the target; skipping execution")
        return
    ctx.require_apply()
    ctx.derive_partitions()

    print("==>", "1/5 partition")
    print(f"{ctx.root_disk}: GPT")
    print(f"  {ctx.part_esp}  EFI System Partition, {ctx.esp_size}")
    print(f"  {ctx.part_root}  {settings.ROOT_LABEL}, remaining space")

    # --zap-all removes both the GPT and the MBR structures. Without the MBR
    # wipe, a disk that previously had a hybrid layout keeps a stale protective
    # MBR that some firmware will still offer as a boot target.
    subprocess.run(["sgdisk", "--zap-all", ctx.root_disk], check=True)

    subprocess.run(
        [
            "sgdisk",
            "--new=1:0:+" + ctx.esp_size,
            "--typecode=1:ef00",
            "--change-name=1:ESP",
            ctx.root_disk,
        ],
        check=True,
    )
    # Keep the root partition GUID reproducible; the UKI selects its GPT label.
    subprocess.run(
        [
            "sgdisk",
            "--new=2:0:0",
            "--typecode=2:8300",
            f"--partition-guid=2:{config.ROOT_PARTUUID}",
            f"--change-name=2:{settings.ROOT_LABEL}",
            ctx.root_disk,
        ],
        check=True,
    )

    # Make the kernel re-read the table, then wait for the nodes to actually
    # appear — mkfs against a partition node that does not exist yet is a race
    # that reproduces about one run in ten.
    if shutil.which("partx"):
        subprocess.run(["partx", "-u", ctx.root_disk], check=False)
    else:
        subprocess.run(["blockdev", "--rereadpt", ctx.root_disk], check=False)
    device.settle_udev()

    for path in (ctx.part_esp, ctx.part_root):
        if not device.exists(path):
            raise BuildError(f"{path} did not appear after partitioning")
    _verify_root_partition(ctx)
    print("ok", "partitions created")


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
    print("ok", f"root partition PARTLABEL: {root.name}")
