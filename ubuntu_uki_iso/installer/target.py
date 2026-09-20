"""Put the system onto the target disk.

Formats the root filesystem, unsquashes the payload onto it, and fixes up the
two things that cannot be known until the disk exists: the fstab, and the
``root=`` UUID on the kernel command line.

That second one is a real trap. The payload was built days earlier on a
different machine, and a command line still carrying ``@@ROOT_UUID@@`` produces
a UKI that builds fine, copies fine, and panics at boot with "unable to find
root device". It is checked three times: here when substituting, in
:mod:`ubuntu_uki_iso.installer.uki` by reading the value back out of the finished
UKI, and again in the fstab writer below.
"""

from __future__ import annotations

from pathlib import Path

from .. import paths, settings
from ..config import boot
from ..errors import BuildError
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
    """Locate what the installer consumes on the medium.

    That is two things now: the payload squashfs, and the kernel packages that
    travel beside it. They are found together because they come from the same
    place — the ISO's ``/payload`` directory — and because an install needs both.

    dracut may have mounted the medium at any of several places depending on
    version and on whether systemd drove it, so this looks for the volume label
    first and falls back to searching the usual mount points — rather than
    guessing one path and being wrong.

    A dry run tolerates not finding either, because the point of a dry run is
    often to check the plan before going near the machine. An apply run has no
    such excuse.
    """
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

    _find_debs(ctx, ctx.payload)


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


def _find_debs(ctx: Context, payload: Path) -> None:
    """The packages that travel beside the payload.

    One, today: the UKI package, carrying the kernel, its modules and the boot
    artifact. There is neither a kernel nor a UKI in the payload itself — the
    target gets both by installing this, which is the same operation as a future
    kernel upgrade on that machine. An install that cannot find it produces a
    machine that boots nothing, which is worth saying before the disk is
    formatted rather than after.
    """
    directory = payload.parent / paths.PAYLOAD_DEBS.name
    debs = sorted(directory.glob("*.deb"))
    if debs:
        ctx.debs = debs
        return

    if ctx.dry_run:
        ctx.console.warn(f"no packages in {directory}; the plan below assumes they exist")
        return

    raise BuildError(
        f"no packages in {directory}.\n"
        "The payload carries neither a kernel nor a UKI: the target gets both by\n"
        "installing the package off the medium, which is the same operation as a\n"
        "kernel upgrade there will be. Without it the install produces a machine\n"
        "that cannot boot."
    )


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

    runner.run("mkdir", "-p", str(ctx.target))
    runner.run("mount", ctx.part_root, str(ctx.target))

    if ctx.dry_run:
        console.grey(f"would unsquashfs {ctx.payload} into {ctx.target}")
        console.grey(f"would write /etc/fstab with root={ctx.part_root} and esp={ctx.part_esp}")
        console.grey("would substitute the real root UUID into /etc/kernel/cmdline")
        return

    if not (ctx.target / "etc").is_dir():
        raise BuildError("the target did not mount where expected")

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

    ctx.load_release()
    if not ctx.release:
        raise BuildError(
            "no kernel release could be derived from the package on the medium.\n"
            "The next step installs it, and the release is what names both the\n"
            "modules directory and the UKI, so there is nothing to do without it."
        )
    console.info(f"kernel: {ctx.release} (from {ctx.uki_package})")

    _write_fstab(ctx)
    _write_cmdline(ctx)

    # A fresh machine identity, so the installed system is distinct from the
    # live environment — journald's persistent storage keys off it.
    runner.run("systemd-machine-id-setup", f"--root={ctx.target}", check=False)

    console.ok("root filesystem prepared")


def _write_fstab(ctx: Context) -> None:
    """Write the fstab, with a note about why the array is mounted nofail.

    The identities here are filesystem UUIDs — generated by mkfs, unique to
    this machine — while the command line names a fixed PARTUUID. That is not
    an inconsistency but the split the design needs: the PARTUUID is fixed
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
        # The mount point is a setting rather than a literal here, because the
        # kernel package's postinst hook points kernel-install's boot root at
        # the same path. Two spellings that drift apart produce a UKI the
        # firmware never looks at.
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
    """Check the payload's command line against the one this build produces.

    Nothing is substituted any more. The installed command line names a fixed
    PARTUUID, so it is complete when the ISO is built and the payload ships it
    already rendered — which is what makes a prebuilt UKI possible at all.

    What is left to do is confirm the payload is the one this build produced.
    The file is not what boots (the UKI's embedded copy is), but it is what any
    later kernel install on the target would read, so a payload whose cmdline
    disagreed with the UKI would be a machine that boots today and panics after
    its first upgrade.
    """
    path = ctx.target / "etc/kernel/cmdline"
    if not path.is_file():
        raise BuildError("the payload has no /etc/kernel/cmdline")

    expected = boot.cmdline_installed()
    actual = path.read_text(encoding="utf-8").strip()

    if actual != expected:
        raise BuildError(
            f"{path} is not the command line this build produces.\n"
            f"  payload:  {actual}\n"
            f"  expected: {expected}\n"
            "The UKI on the medium was built from the expected one. A payload\n"
            "carrying anything else would boot today and build a UKI that panics\n"
            "at the first kernel upgrade."
        )

    if "@@" in actual:
        raise BuildError(
            f"{path} still contains an unsubstituted placeholder: {actual}\n"
            "The UKI built from this would not find its root filesystem."
        )
    ctx.console.ok(f"cmdline: {actual}")
