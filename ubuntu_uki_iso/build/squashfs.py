"""Compress both rootfs images into the ISO staging tree.

::

    /live/filesystem.squashfs      the booted live environment
    /payload/rootfs.squashfs       the installed system, unsquashed by the
                                   installer onto the target disk

The live path is Debian-conventional, which is why it needs the explicit
``rd.live.dir`` and ``rd.live.squashimg`` overrides on the command line:
dracut's own default is ``/LiveOS/squashfs.img``. Change either path in
:mod:`ubuntu_uki_iso.paths` and ``data/cmdline/live`` has to change with it.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from .. import paths
from ..errors import BuildError
from ..log import Console, get_console
from ..paths import Layout
from ..proc import Runner

#: Everything a running system owns, plus package-manager scratch that would
#: be dead weight on the medium. /boot is kept — the kernel and its modules
#: live there and the installed payload needs them.
EXCLUDES = (
    "proc",
    "sys",
    "dev",
    "tmp",
    "run",
    "var/cache/apt",
    "var/lib/apt/lists",
    "var/tmp",
    "boot/efi",
    "etc/mtab",
)


def _squash(
    source: Path,
    destination: Path,
    label: str,
    console: Console,
    runner: Runner,
) -> Path:
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.unlink(missing_ok=True)

    argv = [
        "mksquashfs",
        str(source),
        str(destination),
        "-comp",
        "zstd",
        "-Xcompression-level",
        "19",
        "-noappend",
        "-no-progress",
        "-e",
        *EXCLUDES,
    ]

    # -all-time makes the filesystem deterministically timestamped, which is
    # what lets two builds of the same tree produce the same squashfs.
    # Honoured only when SOURCE_DATE_EPOCH is set, so an ordinary interactive
    # build keeps real timestamps and stays inspectable.
    epoch = os.environ.get("SOURCE_DATE_EPOCH")
    if epoch:
        argv += ["-all-time", epoch]

    console.step(f"squashfs: {label}")
    if epoch:
        console.grey(f"  SOURCE_DATE_EPOCH={epoch}: timestamps pinned")

    runner.run(*argv, check=True)
    console.info(f"{destination.stat().st_size // 1024 // 1024} MB  {destination}")
    return destination


def build(layout: Layout, console: Console, runner: Runner) -> dict[str, Path]:
    for variant, path in (("live", layout.rootfs_live), ("installed", layout.rootfs_installed)):
        if not path.is_dir():
            raise BuildError(
                f"no {variant} rootfs at {path}. Run `ubuntu-uki-iso build rootfs` first."
            )

    return {
        "live": _squash(
            layout.rootfs_live, layout.iso_stage / paths.LIVE_SQUASHFS, "live", console, runner
        ),
        "installed": _squash(
            layout.rootfs_installed,
            layout.iso_stage / paths.PAYLOAD_SQUASHFS,
            "installed",
            console,
            runner,
        ),
    }


def main(argv: list[str] | None = None) -> int:
    console = get_console()
    layout = Layout()
    runner = Runner(dry_run=False, console=console)
    runner.require("mksquashfs")
    build(layout, console, runner)
    console.step("squashfs: done")
    return 0


if __name__ == "__main__":
    sys.exit(main())
