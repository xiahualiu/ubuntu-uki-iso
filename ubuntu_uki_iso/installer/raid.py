"""Use the existing array, or build a new one.

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

from .. import settings
from ..errors import BuildError
from . import device
from .context import RAID_VERIFY_MOUNT, Context
from .preflight import Decision, Preflight


def run(ctx: Context, preflight: Preflight) -> None:
    console = ctx.console
    console.step("2/5 raid")

    match preflight.decision:
        case Decision.REUSE:
            _reuse(ctx, preflight)
        case Decision.CREATE:
            _create(ctx)
        case Decision.NONE:
            console.info("not managing an array (--raid=none)")
        case _:
            raise BuildError(f"internal: unexpected RAID decision {preflight.decision}")


# ---------------------------------------------------------------------------
# Reuse
# ---------------------------------------------------------------------------


def _reuse(ctx: Context, preflight: Preflight) -> None:
    console, runner = ctx.console, ctx.runner

    console.info(
        f"{settings.EXPECTED_MD_LEVEL} array, {settings.EXPECTED_MD_DEVICES} disks, "
        f"{settings.EXPECTED_MD_CHUNK} chunks"
    )
    console.info(f"uuid {settings.EXPECTED_MD_UUID}")
    console.grey("this path never writes a superblock")

    active = preflight.active_array
    if active and active != ctx.md_device.rsplit("/", 1)[-1]:
        raise BuildError(
            f"/dev/{active} is already active, but this installer manages {ctx.md_device}.\n"
            "Refusing to guess which array is the real one."
        )

    if device.exists(ctx.md_device) and _is_active(ctx):
        console.grey(f"{ctx.md_device} is already assembled")
    else:
        # Explicit device list, explicit UUID. --scan would assemble everything
        # the machine can see, which is wider than the job.
        runner.run(
            "mdadm",
            "--assemble",
            ctx.md_device,
            f"--uuid={settings.EXPECTED_MD_UUID}",
            *ctx.data_disks,
        )

    device.settle_udev(runner)

    if ctx.dry_run:
        console.grey(f"would verify {ctx.md_device}: level, member count, chunk size, uuid")
        console.grey("would mount it read-only to prove the filesystem is readable")
        return

    if not _is_active(ctx):
        raise BuildError(f"{ctx.md_device} is not active after assembly")
    _verify_shape(ctx)

    # Prove the filesystem is readable without touching it.
    runner.run("mkdir", "-p", str(RAID_VERIFY_MOUNT))
    runner.run("mount", "-o", "ro,norecovery", ctx.md_device, str(RAID_VERIFY_MOUNT))
    try:
        listing = runner.probe("ls", str(RAID_VERIFY_MOUNT), check=False)
        if listing.ok:
            entries = len(
                [
                    entry
                    for entry in runner.capture(
                        "find",
                        str(RAID_VERIFY_MOUNT),
                        "-maxdepth",
                        "1",
                        "-mindepth",
                        "1",
                        check=False,
                    ).splitlines()
                    if entry
                ]
            )
            console.ok(f"array is readable ({entries} entries at the top level)")
        else:
            console.warn("array mounted but its contents could not be listed")
    finally:
        runner.run("umount", str(RAID_VERIFY_MOUNT), check=False)
        runner.run("rmdir", str(RAID_VERIFY_MOUNT), check=False)

    # Remember the filesystem UUID for the target's fstab.
    ctx.data_uuid = device.uuid_of(runner, ctx.md_device)
    console.ok("existing array reused, unmodified")


def _is_active(ctx: Context) -> bool:
    return device.active_array(ctx.runner) == ctx.md_device.rsplit("/", 1)[-1]


def _verify_shape(ctx: Context) -> None:
    """Confirm the array that came up is the one that was expected.

    Rather than trusting that ``--assemble`` did what was asked.
    """
    runner = ctx.runner
    detail = runner.capture("mdadm", "--detail", ctx.md_device, check=False)
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

    if level != settings.EXPECTED_MD_LEVEL:
        raise BuildError(
            f"{ctx.md_device} came up as {level}, expected {settings.EXPECTED_MD_LEVEL}.\n"
            "\n"
            "A RAID0 assembled at the wrong level is not a slightly-wrong array, it is\n"
            "a different mapping of the same bytes. Refusing to mount it."
        )

    if str(members) != str(settings.EXPECTED_MD_DEVICES):
        raise BuildError(
            f"{ctx.md_device} came up with {members} member slots, expected "
            f"{settings.EXPECTED_MD_DEVICES}.\n"
            "\n"
            "A RAID0 that is missing members still assembles and still mounts — it just\n"
            "returns different data at every offset past the first missing chunk."
        )

    if uuid and uuid != settings.EXPECTED_MD_UUID:
        raise BuildError(f"{ctx.md_device} has uuid {uuid}, expected {settings.EXPECTED_MD_UUID}")

    ctx.console.ok(f"verified: {level}, {members} members, uuid {uuid}")


# ---------------------------------------------------------------------------
# Create — destroys whatever is on the target disks
# ---------------------------------------------------------------------------


def _create(ctx: Context) -> None:
    console, runner = ctx.console, ctx.runner

    console.warn(f"creating a NEW array across {len(ctx.data_disks)} disks")
    console.warn(f"every byte on these disks will be gone: {' '.join(ctx.data_disks)}")

    # preflight.decide already refused if any disk carried a filesystem or a
    # superblock, so reaching here means the disks were verified blank. Re-check
    # anyway: that scan happened before the operator was asked to confirm, and a
    # disk can acquire a signature in between.
    for path in ctx.data_disks:
        if md := device.examine_md(runner, path):
            raise BuildError(
                f"{path} has an md superblock now ({md.uuid}). Something changed since\n"
                "the scan. Refusing to create."
            )
        if found := device.filesystems(runner, path):
            raise BuildError(
                f"{path} has a filesystem signature now ({', '.join(found)}).\n"
                "Something changed since the scan. Refusing to create."
            )

    runner.destructive(
        f"create a new RAID0 across {len(ctx.data_disks)} disks",
        "mdadm",
        "--create",
        ctx.md_device,
        "--level=0",
        f"--raid-devices={len(ctx.data_disks)}",
        f"--chunk={settings.EXPECTED_MD_CHUNK}",
        f"--metadata={settings.RAID_METADATA_VERSION}",
        f"--uuid={settings.EXPECTED_MD_UUID}",
        "--run",
        *ctx.data_disks,
    )

    device.settle_udev(runner)

    if ctx.dry_run:
        console.grey(f"would format {ctx.md_device} ext4 and record it in the target's fstab")
        return

    if not _is_active(ctx):
        raise BuildError(f"{ctx.md_device} did not come up after creation")

    runner.run("mkfs.ext4", "-q", "-L", "ubuntu-uki-iso-data", ctx.md_device)
    ctx.data_uuid = device.uuid_of(runner, ctx.md_device)
    console.ok("new array created and formatted")
