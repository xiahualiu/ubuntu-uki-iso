"""Build the target's UKI here, and pack it the way it will be installed.

The UKI is one PE binary containing the kernel, the initramfs and the command
line. Building it here rather than on the machine that boots it is what makes
the artifact testable: it exists, it can be read back, and it can be booted in
a VM before any disk is touched. The target's job becomes installing a package.

Two things this buys, and one it costs.

It buys an install that is over in seconds — no dracut, no ukify, nothing
compiled — and it buys the property that the first install and every later
kernel update are *the same operation*: installing a version of this package.
There is no path that runs once and is therefore never exercised again.

What it costs is the ability to look at the machine's hardware. That is not a
detail: dracut's host-only mode could see the target's disks, and this build
cannot, because the machine running it is a container on a CI runner. So the
initramfs is built from an explicit list of modules
(``data/dracut/20-uki-build.conf``), and the list is checked here rather than
trusted — see :func:`_verify_initramfs`.

The package contents are deliberate, and one omission is worth stating: there
is **no** ``/boot/vmlinuz-<release>``. The kernel image is already inside the
UKI, and a second copy of the same bytes on the target is a thing that can
disagree with the first.
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

from .. import paths, settings
from ..config import boot
from ..errors import BuildError
from ..log import Console, get_console
from ..paths import Layout
from ..proc import Runner
from ..shim import PACKAGE_LIB, render_shim

#: Scratch, under the workspace's ``out/work``. Three subdirectories, and the
#: separation is what keeps the omitted vmlinuz omitted: the extracted tree is
#: read from, the package root is written to, and nothing is copied wholesale
#: from one to the other.
STAGE = "uki-target"
_EXTRACTED = "extract"
_PKGROOT = "pkgroot"

#: What the target's initramfs must be able to load, and must not.
#:
#: From ``out/kernel/base.config``: these three are modules, so a UKI without
#: them boots to a kernel that cannot see its own root filesystem. Everything
#: else the target needs is built in (ATA, BLK_DEV_SD, MD, EXT4_FS are all
#: ``=y``). nouveau is in the live initramfs's omit list for the same reason it
#: is omitted here — it belongs in the root filesystem, not in early boot.
REQUIRED_MODULES = ("nvme", "ahci", "raid0")
FORBIDDEN_MODULES = ("nouveau",)

#: Paths inside the package, relative to its root.
PACKAGE_UKI = Path("usr/lib/ubuntu-uki-iso/uki.efi")
PACKAGE_RELEASE = Path("usr/lib/ubuntu-uki-iso/release")
PACKAGE_LIBDIR = Path("usr/lib/ubuntu-uki-iso/ubuntu_uki_iso")
DEBIAN = Path("DEBIAN")

#: The package name lives in settings, and the path it is written to in
#: :attr:`~ubuntu_uki_iso.paths.Layout.uki_package`, so the build and the ISO
#: staging cannot disagree about which file is the artifact.


def _run_ukify(stage: Path, release: str, initramfs: Path, runner: Runner) -> Path:
    """Assemble the UKI from the extracted kernel and the built initramfs."""
    vmlinuz = stage / _EXTRACTED / "boot" / f"vmlinuz-{release}"
    if not vmlinuz.is_file():
        raise BuildError(f"the kernel package has no boot/vmlinuz-{release}")

    output = stage / _PKGROOT / PACKAGE_UKI
    output.parent.mkdir(parents=True, exist_ok=True)

    runner.run(
        "ukify",
        "build",
        f"--linux={vmlinuz}",
        f"--initrd={initramfs}",
        f"--cmdline={boot.cmdline_installed()}",
        f"--uname={release}",
        f"--output={output}",
        check=True,
    )
    return output


def _verify_initramfs(initramfs: Path, console: Console, runner: Runner) -> None:
    """Prove the module list actually made it into the initramfs.

    This is the check that replaces "dracut looked at the machine". The list in
    ``20-uki-build.conf`` is a claim about hardware this build has never seen,
    and the failure it guards against — a UKI that boots to a kernel which
    cannot find its root filesystem — is invisible until the machine reboots.
    So the claim is read back out of the artifact instead of being trusted.
    """
    listing = runner.probe("lsinitrd", str(initramfs), check=False)
    if not listing.ok:
        raise BuildError(
            f"could not read {initramfs} with lsinitrd:\n{listing.stderr.strip()}\n"
            "The initramfs is what makes the UKI bootable, so it is not handed on unread."
        )

    # Matches .ko, .ko.zst and .ko.xz — the module's name is what matters, not
    # how the initramfs happens to have compressed it.
    text = listing.stdout
    missing = [name for name in REQUIRED_MODULES if f"{name}.ko" not in text]
    if missing:
        raise BuildError(
            f"the initramfs has no {' '.join(missing)} module(s).\n"
            "\n"
            "They are named in data/dracut/20-uki-build.conf, and this build cannot\n"
            "probe the target's hardware — the list is the only thing keeping the\n"
            "machine able to see its own disks. Either the list is wrong for the\n"
            "hardware, or dracut did not honour it."
        )

    unexpected = [name for name in FORBIDDEN_MODULES if f"{name}.ko" in text]
    if unexpected:
        raise BuildError(
            f"the initramfs contains {' '.join(unexpected)}, which every initramfs in\n"
            "this project excludes.\n"
            "\n"
            "More to the point: it means the whitelist was not applied, so the\n"
            "initramfs is dracut's default set rather than the one this project\n"
            "declared — and the modules that ought to be there cannot be assumed\n"
            "either. Check that drivers+= in data/dracut/20-uki-build.conf is honoured."
        )

    console.ok(
        f"initramfs carries {' '.join(REQUIRED_MODULES)}, and no {' '.join(FORBIDDEN_MODULES)}"
    )


def _write_control(root: Path, release: str) -> None:
    """Write the package metadata.

    ``X-Kernel-Release`` is the field the installer reads, and it is separate
    from ``Version`` on purpose: Version has to change for a repackaging that
    does not rebuild the kernel, while the release — which names
    ``/lib/modules/<release>`` and the UKI's filename — must not.
    """
    control = root / DEBIAN / "control"
    control.parent.mkdir(parents=True, exist_ok=True)
    control.write_text(
        f"Package: {settings.PACKAGE_NAME}\n"
        f"Version: {release}\n"
        f"Architecture: {settings.DPKG_ARCH}\n"
        f"Maintainer: {settings.PACKAGE_MAINTAINER}\n"
        # The postinst is Python and runs on the install path, so this is a
        # real dependency — and declaring it is what makes the build-time
        # offline check (hooks/installed.py) mean something: apt has to find
        # the interpreter inside the payload, with no lists and no network.
        "Depends: python3-minimal\n"
        f"X-Kernel-Release: {release}\n"
        "Section: admin\n"
        "Priority: optional\n"
        f"Description: UKI and kernel modules for {settings.BOOT_LABEL}\n"
        " The kernel image, its initramfs and its command line, as one signed-less\n"
        " PE binary the firmware boots directly, together with the modules that go\n"
        " with it. There is no bootloader: the file is the boot configuration.\n"
        " .\n"
        " Installing this package is how the machine gets a kernel, and installing\n"
        " a newer version of it is how the machine gets a new one.\n",
        encoding="utf-8",
    )


def _write_postinst(root: Path) -> None:
    """Generate the postinst that puts the UKI on the ESP.

    A shim rather than a script: the logic is
    :mod:`ubuntu_uki_iso.ukis.postinst`, which is importable and tested, and
    what dpkg runs is small enough to read in full.
    """
    render = render_shim(
        "ubuntu_uki_iso.ukis.postinst",
        "Place the packaged UKI on the EFI system partition.",
        lib=PACKAGE_LIB,
    )
    path = root / DEBIAN / "postinst"
    path.write_text(render, encoding="utf-8")
    path.chmod(0o755)


def _assemble(stage: Path, release: str, layout: Layout, console: Console, runner: Runner) -> Path:
    """Fill the package root, then hand it to dpkg-deb."""
    root = stage / _PKGROOT

    # -- the modules, and only the modules -------------------------------
    # Moved, not copied wholesale: the extracted tree also holds
    # boot/vmlinuz-<release>, which the UKI already contains and the package
    # deliberately does not ship. Taking the subtree that is wanted, rather
    # than excluding the one that is not, is what keeps that true.
    source_modules = stage / _EXTRACTED / "lib/modules" / release
    if not source_modules.is_dir():
        raise BuildError(f"the kernel package has no lib/modules/{release}")
    destination_modules = root / "lib/modules" / release
    destination_modules.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(source_modules), str(destination_modules))

    # -- the tree the postinst calls into --------------------------------
    # Its own copy, not the payload's: importing the installed copy would run
    # the *old* placement logic from the version being replaced.
    source_package = Path(__file__).resolve().parents[1]
    shutil.copytree(
        source_package,
        root / PACKAGE_LIBDIR,
        ignore=shutil.ignore_patterns("__pycache__"),
    )

    (root / PACKAGE_RELEASE).write_text(f"{release}\n", encoding="utf-8")
    _write_control(root, release)
    _write_postinst(root)

    # -- the UKI ----------------------------------------------------------
    # Copied in by _run_ukify, which ran before this so a failure there does
    # not leave a half-built package root to reason about.
    if not (root / PACKAGE_UKI).is_file():
        raise BuildError(f"no UKI was built into {root / PACKAGE_UKI}")

    output = layout.uki_package
    output.unlink(missing_ok=True)
    console.step(f"uki-target: {output.name}")

    # --root-owner-group so the archive is root:root regardless of who built
    # it, which is what makes two builds of the same tree comparable.
    runner.run(
        "dpkg-deb",
        "--build",
        "--root-owner-group",
        str(root),
        str(output),
        check=True,
    )
    return output


def build(layout: Layout, console: Console, runner: Runner) -> Path:
    """Build the target's UKI, package it, and return the package."""
    release = layout.kernel_release()
    image = layout.kernel_image_deb()

    stage = layout.work / STAGE
    if stage.exists():
        shutil.rmtree(stage)
    (stage / _EXTRACTED).mkdir(parents=True)
    (stage / _PKGROOT).mkdir(parents=True)

    console.step(f"uki-target: extract {image.name}")
    console.info(f"kernel {release}")
    # Extracted, never installed: dpkg would run the kernel's maintainer
    # scripts, which on a machine that is not the target is both pointless and
    # a mutation of the machine running the build.
    runner.run("dpkg-deb", "-x", str(image), str(stage / _EXTRACTED), check=True, quiet=True)

    console.step("uki-target: dracut")
    console.grey("not host-only, and limited to the modules in 20-uki-build.conf")
    moddir = stage / _EXTRACTED / "lib/modules" / release
    initramfs = stage / f"initramfs-{release}.img"
    runner.run(
        "dracut",
        "--no-hostonly",
        "--force",
        f"--kver={release}",
        # The kernel module directory itself, not its parent: dracut checks
        # both that the last path component is the kernel version and that the
        # parent ends in /lib/modules/.
        f"--kmoddir={moddir}",
        f"--conf={paths.data_file('dracut', '20-uki-build.conf')}",
        str(initramfs),
        check=True,
    )

    _verify_initramfs(initramfs, console, runner)

    console.step("uki-target: ukify")
    _run_ukify(stage, release, initramfs, runner)

    deb = _assemble(stage, release, layout, console, runner)
    console.info(f"{deb.stat().st_size // 1024 // 1024} MB  {deb}")
    return deb


def main(argv: list[str] | None = None) -> int:
    console = get_console()
    layout = Layout()
    runner = Runner(dry_run=False, console=console)
    runner.require("dpkg-deb", "dracut", "ukify", "lsinitrd")
    layout.require_dirs()

    build(layout, console, runner)
    return 0


if __name__ == "__main__":
    sys.exit(main())
