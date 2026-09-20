"""Build the two rootfs images with mmdebstrap.

``--variant=minbase`` is the smallest starting point that is still a working
dpkg/apt system; everything else is named explicitly. Recommends are off,
because that is how a "minimal server" quietly acquires a desktop stack.

The two images differ in one large way. The live image installs the custom
kernel, because the ISO's own UKI is built from it. The installed image has no
kernel at all: the target gets one by installing the packages the medium
carries, which is the same operation as a kernel upgrade on that machine.

Both still need the kernel build to have happened — the live image to install
the packages, the installed image so that its hook can prove they resolve
against it.
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

from .. import settings
from ..config import host_packages, package_list
from ..errors import BuildError
from ..log import Console, get_console
from ..paths import Layout
from ..proc import Runner

VARIANTS = ("live", "installed")


def build(
    variant: str,
    layout: Layout,
    console: Console,
    runner: Runner,
) -> Path:
    """Bootstrap one rootfs. Returns the directory it was built into."""
    if variant not in VARIANTS:
        raise BuildError(f"unknown rootfs variant {variant!r}; expected one of {VARIANTS}")

    target: Path = layout.rootfs_live if variant == "live" else layout.rootfs_installed
    packages = package_list(variant)

    console.step(f"rootfs: {variant} -> {target}")
    console.info(f"{len(packages)} packages")

    if target.exists():
        shutil.rmtree(target)

    # The hook runs in mmdebstrap's process, not ours, so it is invoked as a
    # module rather than called directly. PYTHONPATH is already set in the
    # container by the entry point.
    hook = f"python3 -m ubuntu_uki_iso.build.hooks.{variant} $1"

    runner.run(
        "mmdebstrap",
        "--arch=amd64",
        "--variant=minbase",
        f"--components={settings.UBUNTU_COMPONENTS}",
        f"--include={packages.comma_joined()}",
        '--aptopt=Apt::Install-Recommends "false"',
        f"--customize-hook={hook}",
        settings.UBUNTU_SUITE,
        str(target),
        settings.UBUNTU_MIRROR,
        check=True,
    )

    if not (target / "etc").is_dir():
        raise BuildError(f"mmdebstrap reported success but {target} is not a rootfs")

    console.info(f"built {_human_size(target)}")
    return target


def _human_size(path: Path) -> str:
    total = float(sum(f.stat().st_size for f in path.rglob("*") if f.is_file()))
    for unit in ("B", "kB", "MB", "GB"):
        if total < 1024:
            return f"{total:.0f} {unit}"
        total /= 1024
    return f"{total:.1f} TB"


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    console = get_console()
    layout = Layout()
    runner = Runner(dry_run=False, console=console)

    which = argv[0] if argv else "all"
    if which not in (*VARIANTS, "all"):
        console.error(f"usage: rootfs [{'|'.join(VARIANTS)}|all]")
        return 2

    runner.require("mmdebstrap", "chroot")
    runner.require_packages(*host_packages("build"))
    layout.require_dirs()

    # Both hooks need the built packages — the live one to install them, the
    # installed one to prove they resolve — so the kernel build has to have
    # happened either way. Checked here rather than inside a hook, where the
    # failure would surface as a mmdebstrap error with no context.
    release = layout.kernel_release()
    console.info(f"kernel: {release}")

    variants = VARIANTS if which == "all" else (which,)
    for variant in variants:
        build(variant, layout, console, runner)

    console.step("rootfs: done")
    return 0


if __name__ == "__main__":
    sys.exit(main())
