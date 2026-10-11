"""Locate the installation payload and deploy its root filesystem."""

from __future__ import annotations

from pathlib import Path

from .. import paths, settings
from ..errors import BuildError
from ..uki import config as boot
from . import device
from .context import Context

#: Where a live medium turns up, in the order worth trying.
MEDIUM_CANDIDATES = (
    "/run/initramfs/live",
    "/run/live/medium",
    "/run/live/rootfs",
    "/media",
    "/mnt",
)


def find_payload(ctx: Context) -> None:
    """Locate and validate the SquashFS, standalone UKI and checksum manifest."""
    if ctx.payload is not None:
        if not ctx.payload.is_file():
            raise BuildError(f"--payload {ctx.payload} does not exist")
    else:
        located = _search_medium(ctx)
        if located is None:
            if not ctx.dry_run:
                raise BuildError(
                    f"could not find the installation payload ({paths.PAYLOAD_SQUASHFS}).\n"
                    f"Looked for volume label {settings.VOLID!r} and under "
                    f"{', '.join(MEDIUM_CANDIDATES)}.\n"
                    "Pass --payload /path/to/rootfs.squashfs if it is somewhere else."
                )
            ctx.console.warn("no installation payload found; the plan below names a placeholder")
            located = Path("<medium>") / paths.PAYLOAD_SQUASHFS
        ctx.payload = located

    directory = ctx.payload.parent
    uki = directory / "uki.efi"
    if not uki.is_file() and ctx.dry_run:
        ctx.console.warn(f"no installed UKI at {uki}; dry-run only")
        return
    from ..media import validate_payload

    ctx.release = validate_payload(directory)
    ctx.source_uki = uki


def _search_medium(ctx: Context) -> Path | None:
    """The payload squashfs, found by volume label or by looking around."""
    source = ctx.runner.capture("blkid", "-L", settings.VOLID, check=False)
    if source:
        mount = ctx.runner.capture("findmnt", "-n", "-o", "TARGET", "--source", source, check=False)
        mountpoint = mount.splitlines()[0].strip() if mount else ""
        if mountpoint:
            candidate = Path(mountpoint) / paths.PAYLOAD_SQUASHFS
            if candidate.is_file():
                return candidate

    for base in MEDIUM_CANDIDATES:
        # /media and /mnt hold one directory per mounted medium; the rest are
        # mount points themselves.
        candidates = sorted(Path(base).glob("*")) if base in ("/media", "/mnt") else [Path(base)]
        for candidate in candidates:
            path = candidate / paths.PAYLOAD_SQUASHFS
            if path.is_file():
                return path
    return None


def run(ctx: Context) -> None:
    console, runner = ctx.console, ctx.runner
    console.step("3/5 rootfs")

    runner.destructive(
        f"format {ctx.part_root} as ext4",
        "mkfs.ext4",
        "-q",
        "-L",
        settings.ROOT_LABEL,
        ctx.part_root,
    )

    runner.run("mkdir", "-p", str(ctx.target), check=True)
    runner.run("mount", ctx.part_root, str(ctx.target), check=True)

    if ctx.dry_run:
        console.grey(f"would unsquashfs {ctx.payload} into {ctx.target}")
        console.grey(f"would write /etc/fstab with root={ctx.part_root} and esp={ctx.part_esp}")
        console.grey("would verify the installed GPT root label and kernel command line")
        return

    if not device.is_mounted(runner, ctx.part_root):
        raise BuildError("the target root partition did not mount")

    # --- payload ----------------------------------------------------------
    if ctx.payload is None:
        raise BuildError("no payload was located")
    size_mb = ctx.payload.stat().st_size // 1024 // 1024
    console.info(f"unsquashing {size_mb} MB payload onto {ctx.part_root}")
    runner.run("unsquashfs", "-f", "-d", str(ctx.target), str(ctx.payload), check=True, quiet=True)

    if not (ctx.target / "etc").is_dir():
        raise BuildError(f"the payload did not unpack a usable root filesystem into {ctx.target}")
    console.ok("root filesystem written")

    # --- identities -------------------------------------------------------
    ctx.root_uuid = device.uuid_of(runner, ctx.part_root)
    if not ctx.root_uuid:
        raise BuildError(f"could not read the UUID of {ctx.part_root}")
    ctx.esp_uuid = device.uuid_of(runner, ctx.part_esp)
    if not ctx.esp_uuid:
        raise BuildError(f"could not read the UUID of {ctx.part_esp}")

    if not ctx.release or not (ctx.target / "usr/lib/modules" / ctx.release).is_dir():
        raise BuildError(f"installed rootfs has no modules for {ctx.release}")
    console.info(f"kernel: {ctx.release} (from the prebuilt UKI)")

    _write_fstab(ctx)
    _write_cmdline(ctx)

    # A fresh machine identity, so the installed system is distinct from the
    # live environment — journald's persistent storage keys off it.
    runner.run("systemd-machine-id-setup", f"--root={ctx.target}", check=False)

    console.ok("root filesystem prepared")


def _write_fstab(ctx: Context) -> None:
    """Write the fstab, with a note about why the array is mounted nofail.

    The identities here are filesystem UUIDs — generated by mkfs, unique to
    this machine — while the command line names a GPT PARTLABEL. That is not
    an inconsistency but the split the design needs: the PARTLABEL is fixed
    because it must be known before the disk exists, and the UUID is random
    because it need not be. Keeping the mount identity random is also what
    stops two disks provisioned from the same ISO from becoming
    indistinguishable once both are formatted.
    """
    lines = [
        "# /etc/fstab — generated by ubuntu-uki-iso install",
        "#",
        "# The data array is mounted with nofail: RAID0 has no redundancy, so a",
        "# single dead member means the array does not assemble at all. Without",
        "# nofail that turns a disk failure into a machine that will not boot.",
        "",
        f"UUID={ctx.root_uuid:<40} /         ext4  defaults        0 1",
        f"UUID={ctx.esp_uuid:<40} {settings.ESP_MOUNT:<9} vfat  umask=0077      0 1",
    ]
    if ctx.data_uuid:
        lines.append(f"UUID={ctx.data_uuid:<40} /mnt/raid0 ext4  defaults,nofail 0 2")
    lines += [
        "",
        "# No swap entry, and no swap of any kind: no swapfile, no swap",
        "# partition, no zram. Under memory pressure the kernel reclaims what",
        "# it can and then OOM-kills. That is the intended behaviour.",
        "",
    ]

    (ctx.target / "etc/fstab").write_text("\n".join(lines), encoding="utf-8")
    for directory in ("mnt/raid0", settings.ESP_MOUNT.lstrip("/")):
        (ctx.target / directory).mkdir(parents=True, exist_ok=True)

    ctx.console.info("fstab:")
    for line in lines:
        if line and not line.startswith("#"):
            ctx.console.info(f"  {line}")


def _write_cmdline(ctx: Context) -> None:
    """Verify that the installed rootfs uses the prebuilt UKI's command line."""
    path = ctx.target / "etc/kernel/cmdline"
    if not path.is_file():
        raise BuildError("the payload has no /etc/kernel/cmdline")

    expected = boot.cmdline("installed")
    actual = path.read_text(encoding="utf-8").strip()

    if actual != expected:
        raise BuildError(
            f"{path} is not the command line this build produces.\n"
            f"  payload:  {actual}\n"
            f"  expected: {expected}\n"
            "The UKI on the medium was built from the expected one. A payload\n"
            "carrying anything else would disagree with its boot artifact."
        )

    if "@@" in actual:
        raise BuildError(
            f"{path} still contains an unsubstituted placeholder: {actual}\n"
            "The UKI built from this would not find its root filesystem."
        )
    ctx.console.ok(f"cmdline: {actual}")
