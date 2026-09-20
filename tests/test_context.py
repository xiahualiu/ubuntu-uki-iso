"""The kernel release, and where the installer gets it from.

The payload carries neither a kernel nor a UKI, so the release cannot be read
out of a directory listing on the target. It comes from the package that is
about to be installed — the only thing on the medium that knows — and everything
downstream keys off it, from the UKI's filename on the ESP to the closing
summary. So the field it comes from is checked against the modules the package
actually ships, because that is the one place the two could disagree.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from conftest import RecordingRunner
from ubuntu_uki_iso import settings
from ubuntu_uki_iso.errors import BuildError
from ubuntu_uki_iso.installer.context import RELEASE_FIELD, Context
from ubuntu_uki_iso.installer.preflight import RaidMode
from ubuntu_uki_iso.log import Console
from ubuntu_uki_iso.paths import Layout

RELEASE = "7.0.14-ubuntu-uki-iso"
UKI_DEB = f"{settings.PACKAGE_NAME}_{RELEASE}_amd64.deb"
OTHER_DEB = "unrelated-package_1.0_amd64.deb"


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


def _stub(runner: RecordingRunner, *, release: str = RELEASE, contents: str | None = None) -> None:
    """Both dpkg-deb calls the release derivation makes, matched on their own
    arguments — the field name for one, ``--contents`` for the other."""
    runner.stub(RELEASE_FIELD, stdout=f"{release}\n")
    listing = f"./lib/modules/{release}/\n" if contents is None else contents
    runner.stub("--contents", stdout=listing)


def test_the_release_comes_from_the_package_field(
    tmp_path: Path, runner: RecordingRunner, console: Console
) -> None:
    """The package's own field, not its file name — a name is only a convention."""
    _stub(runner)
    ctx = _context(tmp_path, runner, console, [UKI_DEB])

    assert ctx.load_release() == RELEASE
    assert ctx.release == RELEASE


def test_every_release_carries_the_localversion(
    tmp_path: Path, runner: RecordingRunner, console: Console
) -> None:
    """The suffix is what makes this kernel distinguishable from the distro's.

    If it were ever dropped, the custom kernel and Ubuntu's would claim the same
    release and their modules would collide in /lib/modules.
    """
    _stub(runner)
    ctx = _context(tmp_path, runner, console, [UKI_DEB])

    assert ctx.load_release().endswith("-ubuntu-uki-iso")


def test_a_package_whose_payload_disagrees_with_its_field_is_refused(
    tmp_path: Path, runner: RecordingRunner, console: Console
) -> None:
    """The failure this guards is a machine that boots and cannot load a driver.

    The UKI would be built for one kernel while the modules on the disk are for
    another, and nothing at boot would say which.
    """
    _stub(runner, contents="./lib/modules/6.9.1-something-else/\n")
    ctx = _context(tmp_path, runner, console, [UKI_DEB])

    with pytest.raises(BuildError, match="ships no"):
        ctx.load_release()


def test_a_package_without_the_field_is_refused(
    tmp_path: Path, runner: RecordingRunner, console: Console
) -> None:
    """Better a refusal than a release guessed from the file name."""
    _stub(runner, release="")
    ctx = _context(tmp_path, runner, console, [UKI_DEB])

    with pytest.raises(BuildError, match=RELEASE_FIELD):
        ctx.load_release()


def test_an_unrelated_package_is_not_mistaken_for_the_uki_package(
    tmp_path: Path, runner: RecordingRunner, console: Console
) -> None:
    ctx = _context(tmp_path, runner, console, [OTHER_DEB])

    assert ctx.uki_package is None
    assert ctx.load_release() == ""


def test_no_packages_at_all_yields_no_release(
    tmp_path: Path, runner: RecordingRunner, console: Console
) -> None:
    """The dry run on a machine with no medium attached still has to work."""
    ctx = _context(tmp_path, runner, console, [])

    assert ctx.load_release() == ""
    assert runner.probed == []
