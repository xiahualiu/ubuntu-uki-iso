"""The kernel release, and where the installer gets it from.

The payload carries no kernel, so the release cannot be read out of a directory
listing on the target any more. It comes from the package that is about to be
installed, which is the only thing on the medium that knows — and everything
downstream keys off it, from ``kernel-install``'s UKI name to the closing
summary.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from conftest import RecordingRunner
from ubuntu_uki_iso.errors import BuildError
from ubuntu_uki_iso.installer.context import IMAGE_PACKAGE_PREFIX, Context
from ubuntu_uki_iso.installer.preflight import RaidMode
from ubuntu_uki_iso.log import Console
from ubuntu_uki_iso.paths import Layout

IMAGE_DEB = "linux-image-7.0.14-ubuntu-uki-iso_7.0.14-ubuntu-uki-iso-1_amd64.deb"
HEADERS_DEB = "linux-headers-7.0.14-ubuntu-uki-iso_7.0.14-ubuntu-uki-iso-1_amd64.deb"
RELEASE = "7.0.14-ubuntu-uki-iso"


def _context(tmp_path: Path, runner: RecordingRunner, console: Console, debs: list[str]) -> Context:
    return Context(
        root_disk="/dev/nvme0n1",
        data_disks=[],
        runner=runner,
        console=console,
        layout=Layout(tmp_path),
        mode=RaidMode.NONE,
        debs=[tmp_path / name for name in debs],
    )


def test_the_release_comes_from_the_package_field(
    tmp_path: Path, runner: RecordingRunner, console: Console
) -> None:
    """``Package:``, not the file name — the name is only a convention."""
    runner.stub("dpkg-deb", stdout=f"{IMAGE_PACKAGE_PREFIX}{RELEASE}\n")
    ctx = _context(tmp_path, runner, console, [IMAGE_DEB, HEADERS_DEB])

    assert ctx.load_release() == RELEASE
    assert ctx.release == RELEASE


def test_every_release_carries_the_localversion(
    tmp_path: Path, runner: RecordingRunner, console: Console
) -> None:
    """The suffix is what makes this kernel distinguishable from the distro's.

    If it were ever dropped, the custom kernel and Ubuntu's would claim the
    same release and the UKI layout would collide.
    """
    runner.stub("dpkg-deb", stdout=f"{IMAGE_PACKAGE_PREFIX}{RELEASE}\n")
    ctx = _context(tmp_path, runner, console, [IMAGE_DEB])

    assert ctx.load_release().endswith("-ubuntu-uki-iso")


def test_a_package_that_is_not_a_kernel_image_is_refused(
    tmp_path: Path, runner: RecordingRunner, console: Console
) -> None:
    """Better a refusal than a release derived from the wrong string."""
    runner.stub("dpkg-deb", stdout="docker.io\n")
    ctx = _context(tmp_path, runner, console, [IMAGE_DEB])

    with pytest.raises(BuildError, match="not a kernel image"):
        ctx.load_release()


def test_the_headers_package_is_not_mistaken_for_the_image(
    tmp_path: Path, runner: RecordingRunner, console: Console
) -> None:
    ctx = _context(tmp_path, runner, console, [HEADERS_DEB])

    assert ctx.image_deb is None
    assert ctx.load_release() == ""


def test_no_packages_at_all_yields_no_release(
    tmp_path: Path, runner: RecordingRunner, console: Console
) -> None:
    """The dry run on a machine with no medium attached still has to work."""
    ctx = _context(tmp_path, runner, console, [])

    assert ctx.load_release() == ""
    assert runner.probed == []
