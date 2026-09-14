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

from .. import settings
from ..config import boot
from ..errors import BuildError
from . import device
from .context import Context

#: Where the payload squashfs lives on the installation medium.
PAYLOAD_RELPATH = "payload/rootfs.squashfs"

#: Where a live medium turns up, in the order worth trying.
MEDIUM_CANDIDATES = (
    "/run/initramfs/live",
    "/run/live/medium",
    "/run/live/rootfs",
    "/media",
    "/mnt",
)


def find_payload(ctx: Context) -> None:
    """Locate the payload squashfs on the installation medium.

    dracut may have mounted the medium at any of several places depending on
    version and on whether systemd drove it, so this looks for the volume label
    first and falls back to searching the usual mount points — rather than
    guessing one path and being wrong.

    A dry run tolerates not finding it, because the point of a dry run is often
    to check the plan before going near the machine. An apply run has no such
    excuse: without the payload there is nothing to install.
    """
    if ctx.payload is not None:
        if not ctx.payload.is_file():
            raise BuildError(f"--payload {ctx.payload} does not exist")
        return

    source = ctx.runner.capture("blkid", "-L", settings.VOLID, check=False)
    if source:
        mount = ctx.runner.capture("findmnt", "-n", "-o", "TARGET", "--source", source, check=False)
        mountpoint = mount.splitlines()[0].strip() if mount else ""
        if mountpoint:
            candidate = Path(mountpoint) / PAYLOAD_RELPATH
            if candidate.is_file():
                ctx.payload = candidate
                return

    for base in MEDIUM_CANDIDATES:
        # /media and /mnt hold one directory per mounted medium; the rest are
        # mount points themselves.
        candidates = sorted(Path(base).glob("*")) if base in ("/media", "/mnt") else [Path(base)]
        for candidate in candidates:
            path = candidate / PAYLOAD_RELPATH
            if path.is_file():
                ctx.payload = path
                return

    if ctx.dry_run:
        ctx.console.warn("no installation payload found; the plan below names a placeholder")
        ctx.payload = Path("<medium>") / PAYLOAD_RELPATH
        return

    raise BuildError(
        f"could not find the installation payload ({PAYLOAD_RELPATH}).\n"
        f"Looked for volume label {settings.VOLID!r} and under "
        f"{', '.join(MEDIUM_CANDIDATES)}.\n"
        "Pass --payload /path/to/rootfs.squashfs if it is somewhere else."
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
        raise BuildError("no kernel modules in the payload — it carries no kernel")
    console.info(f"kernel: {ctx.release}")

    _write_fstab(ctx)
    _write_cmdline(ctx)

    # A fresh machine identity, so the installed system is distinct from the
    # live environment — journald's persistent storage and the UKI filename
    # both key off it.
    runner.run("systemd-machine-id-setup", f"--root={ctx.target}", check=False)

    console.ok("root filesystem prepared")


def _write_fstab(ctx: Context) -> None:
    """Write the fstab, with a note about why the array is mounted nofail."""
    lines = [
        "# /etc/fstab — generated by ubuntu-uki-iso install",
        "#",
        "# The data array is mounted with nofail: RAID0 has no redundancy, so a",
        "# single dead member means the array does not assemble at all. Without",
        "# nofail that turns a disk failure into a machine that will not boot.",
        "",
        f"UUID={ctx.root_uuid:<40} /         ext4  defaults        0 1",
        f"UUID={ctx.esp_uuid:<40} /boot/efi vfat  umask=0077      0 1",
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
    for directory in ("mnt/raid0", "boot/efi"):
        (ctx.target / directory).mkdir(parents=True, exist_ok=True)

    ctx.console.info("fstab:")
    for line in lines:
        if line and not line.startswith("#"):
            ctx.console.info(f"  {line}")


def _write_cmdline(ctx: Context) -> None:
    """Substitute the real root UUID into the command line, then check."""
    path = ctx.target / "etc/kernel/cmdline"
    if not path.is_file():
        raise BuildError("the payload has no /etc/kernel/cmdline")

    ctx.console.info(f"cmdline: {path.read_text().strip()}  ->  substituting root UUID")

    rendered = boot.render_installed_cmdline(ctx.root_uuid)
    path.write_text(rendered + "\n", encoding="utf-8")

    # A placeholder that survived is a UKI that panics at boot, so this is
    # checked rather than assumed.
    if "@@" in rendered:
        raise BuildError(
            f"{path} still contains an unsubstituted placeholder: {rendered}\n"
            "The UKI built from this would not find its root filesystem."
        )
    ctx.console.ok(f"cmdline: {rendered}")
