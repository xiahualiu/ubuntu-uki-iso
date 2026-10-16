"""
Use the existing array, or build a new one.

The reuse path is the one that runs in normal use, and it is built to leave no
trace: no ``--create``, no ``--zero-superblock``, no ``--update``, no
``--force``. It verifies the array it found is the array it expected and hands
it on.

The verification mount is read-only *with* ``norecovery``. A plain ``mount -o
ro`` on ext4 still replays the journal, which is a write to the filesystem —
and the whole promise of this path is that the array comes out exactly as it
went in.
"""

from __future__ import annotations

import subprocess
import sys

from ..errors import BuildError
from . import config, device
from .context import RAID_VERIFY_MOUNT, Context
from .preflight import Decision, Preflight


def run(ctx: Context, preflight: Preflight) -> None:
    if ctx.dry_run:
        print("dry run: this step would modify the target; skipping execution")
        return
    ctx.require_apply()
    print("==>", "2/5 raid")

    match preflight.decision:
        case Decision.REUSE:
            _reuse(ctx, preflight)
        case Decision.CREATE:
            _create(ctx)
        case Decision.NONE:
            print("not managing an array (--raid=none)")
        case _:
            raise BuildError(f"internal: unexpected RAID decision {preflight.decision}")


# ---------------------------------------------------------------------------
# Reuse
# ---------------------------------------------------------------------------


def _reuse(ctx: Context, preflight: Preflight) -> None:
    if ctx.dry_run:
        print("dry run: this step would modify the target; skipping execution")
        return
    ctx.require_apply()

    print(
        f"{config.EXPECTED_MD_LEVEL} array, {len(ctx.data_disks)} disks, "
        f"{config.EXPECTED_MD_CHUNK} chunks"
    )
    print(f"uuid {config.EXPECTED_MD_UUID}")
    print("this path never writes a superblock")

    active = preflight.active_array
    if active and active != ctx.md_device.rsplit("/", 1)[-1]:
        raise BuildError(
            f"/dev/{active} is already active, but this installer manages {ctx.md_device}.\n"
            "Refusing to guess which array is the real one."
        )

    if device.exists(ctx.md_device) and _is_active(ctx):
        print(f"{ctx.md_device} is already assembled")
    else:
        # Explicit device list, explicit UUID. --scan would assemble everything
        # the machine can see, which is wider than the job.
        subprocess.run(
            [
                "mdadm",
                "--assemble",
                ctx.md_device,
                f"--uuid={config.EXPECTED_MD_UUID}",
                *ctx.data_disks,
            ],
            check=True,
        )

    device.settle_udev()

    if not _is_active(ctx):
        raise BuildError(f"{ctx.md_device} is not active after assembly")
    _verify_shape(ctx)

    # A mounted, matching array is left mounted and unchanged. Only inspect it
    # to record the UUID used by the installed system's optional fstab entry.
    if device.is_mounted(ctx.md_device):
        ctx.data_uuid = device.uuid_of(ctx.md_device)
        if not ctx.data_uuid:
            raise BuildError(f"could not read the filesystem UUID of {ctx.md_device}")
        print("ok", "existing array is already mounted; left unchanged")
        return

    # Prove the filesystem is readable without touching it.
    subprocess.run(["mkdir", "-p", str(RAID_VERIFY_MOUNT)], check=True)
    subprocess.run(
        ["mount", "-o", "ro,norecovery", ctx.md_device, str(RAID_VERIFY_MOUNT)], check=True
    )
    try:
        listing = subprocess.run(
            ["ls", str(RAID_VERIFY_MOUNT)], check=False, capture_output=True, text=True, timeout=120
        )
        if listing.returncode == 0:
            entries = len(
                [
                    entry
                    for entry in subprocess.run(
                        ["find", str(RAID_VERIFY_MOUNT), "-maxdepth", "1", "-mindepth", "1"],
                        check=False,
                        capture_output=True,
                        text=True,
                        timeout=120,
                    )
                    .stdout.strip()
                    .splitlines()
                    if entry
                ]
            )
            print("ok", f"array is readable ({entries} entries at the top level)")
        else:
            print("WARN:", "array mounted but its contents could not be listed", file=sys.stderr)
    finally:
        subprocess.run(["umount", str(RAID_VERIFY_MOUNT)], check=False)
        subprocess.run(["rmdir", str(RAID_VERIFY_MOUNT)], check=False)

    # Remember the filesystem UUID for the target's fstab.
    ctx.data_uuid = device.uuid_of(ctx.md_device)
    if not ctx.data_uuid:
        raise BuildError(f"could not read the filesystem UUID of {ctx.md_device}")
    print("ok", "existing array reused, unmodified")


def _is_active(ctx: Context) -> bool:
    return device.active_array() == ctx.md_device.rsplit("/", 1)[-1]


def _verify_shape(ctx: Context) -> None:
    """
    Confirm the array that came up is the one that was expected.

    Rather than trusting that ``--assemble`` did what was asked.
    """
    detail = subprocess.run(
        ["mdadm", "--detail", ctx.md_device],
        check=False,
        capture_output=True,
        text=True,
        timeout=120,
    ).stdout.strip()
    if not detail:
        raise BuildError(f"mdadm --detail {ctx.md_device} produced nothing")

    fields = {}
    for line in detail.splitlines():
        key, separator, value = line.partition(":")
        if separator:
            fields[key.strip()] = value.strip()

    level = fields.get("Raid Level", "")
    members = fields.get("Raid Devices", "")
    uuid = fields.get("UUID", "")

    if not level or not members:
        raise BuildError(f"could not read the shape of {ctx.md_device}")

    if level != config.EXPECTED_MD_LEVEL:
        raise BuildError(
            f"{ctx.md_device} came up as {level}, expected {config.EXPECTED_MD_LEVEL}.\n"
            "\n"
            "A RAID0 assembled at the wrong level is not a slightly-wrong array, it is\n"
            "a different mapping of the same bytes. Refusing to mount it."
        )

    if str(members) != str(len(ctx.data_disks)):
        raise BuildError(
            f"{ctx.md_device} came up with {members} member slots, expected "
            f"{len(ctx.data_disks)}.\n"
            "\n"
            "A RAID0 that is missing members still assembles and still mounts — it just\n"
            "returns different data at every offset past the first missing chunk."
        )

    if uuid and uuid != config.EXPECTED_MD_UUID:
        raise BuildError(f"{ctx.md_device} has uuid {uuid}, expected {config.EXPECTED_MD_UUID}")

    print("ok", f"verified: {level}, {members} members, uuid {uuid}")


# ---------------------------------------------------------------------------
# Create — destroys whatever is on the target disks
# ---------------------------------------------------------------------------


def _create(ctx: Context) -> None:
    if ctx.dry_run:
        print("dry run: this step would modify the target; skipping execution")
        return
    ctx.require_apply()

    print("WARN:", f"creating a NEW array across {len(ctx.data_disks)} disks", file=sys.stderr)
    print(
        "WARN:",
        f"every byte on these disks will be gone: {' '.join(ctx.data_disks)}",
        file=sys.stderr,
    )

    # preflight.decide already refused if any disk carried a filesystem or a
    # superblock, so reaching here means the disks were verified blank. Re-check
    # anyway: that scan happened before the operator was asked to confirm, and a
    # disk can acquire a signature in between.
    for path in ctx.data_disks:
        if md := device.examine_md(path):
            raise BuildError(
                f"{path} has an md superblock now ({md.uuid}). Something changed since\n"
                "the scan. Refusing to create."
            )
        if found := device.filesystems(path):
            raise BuildError(
                f"{path} has a filesystem signature now ({', '.join(found)}).\n"
                "Something changed since the scan. Refusing to create."
            )

    subprocess.run(
        [
            "mdadm",
            "--create",
            ctx.md_device,
            "--level=0",
            f"--raid-devices={len(ctx.data_disks)}",
            f"--chunk={config.EXPECTED_MD_CHUNK}",
            f"--metadata={config.RAID_METADATA_VERSION}",
            f"--uuid={config.EXPECTED_MD_UUID}",
            "--run",
            *ctx.data_disks,
        ],
        check=True,
    )

    device.settle_udev()

    if not _is_active(ctx):
        raise BuildError(f"{ctx.md_device} did not come up after creation")

    subprocess.run(["mkfs.ext4", "-q", "-L", "ubuntu-uki-iso-data", ctx.md_device], check=True)
    ctx.data_uuid = device.uuid_of(ctx.md_device)
    if not ctx.data_uuid:
        raise BuildError(f"could not read the filesystem UUID of {ctx.md_device}")
    print("ok", "new array created and formatted")
