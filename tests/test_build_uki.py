"""The ISO's UKI: where it is harvested from, and what is accepted.

The build half of this project had no unit tests before this file, because
almost all of it is shelling out to things that need a container. What is left
over is the part that can be wrong quietly — where kernel-install puts the UKI,
and which command lines the verification will accept — so that is what is
covered here.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from helpers import make_uki
from ubuntu_uki_iso.build.uki import _harvest, _verify
from ubuntu_uki_iso.errors import BuildError
from ubuntu_uki_iso.log import Console
from ubuntu_uki_iso.paths import Layout


def _staged(staging: Path, relative: str, cmdline: bytes) -> Path:
    path = staging / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(make_uki(cmdline))
    return path


# ---------------------------------------------------------------------------
# The harvest
# ---------------------------------------------------------------------------


def test_the_harvest_finds_the_uki_under_efi_linux(tmp_path: Path) -> None:
    """Where 90-uki-copy.install puts it, which is not the staging root.

    This is the layout bug this test exists for: harvesting ``*.efi`` from the
    staging directory finds nothing at all, because kernel-install keeps its
    layout under an ``EFI/`` tree, and the build then fails after a successful
    run of the expensive step.
    """
    staging = tmp_path / "esp-staging"
    _staged(staging, "EFI/Linux/0123456789abcdef-7.0.14-ubuntu-uki-iso.efi", b"root=live:x")
    _staged(staging, "EFI/BOOT/BOOTX64.EFI", b"root=live:x")

    harvested = _harvest(staging)

    assert harvested.parent.name == "Linux"
    assert harvested.name == "0123456789abcdef-7.0.14-ubuntu-uki-iso.efi"


def test_the_harvest_does_not_take_the_fallback_copy(tmp_path: Path) -> None:
    """The fallback plugin's copy is in the same tree and is not the answer."""
    staging = tmp_path / "esp-staging"
    _staged(staging, "EFI/BOOT/BOOTX64.EFI", b"root=live:x")

    with pytest.raises(BuildError, match="EFI/Linux"):
        _harvest(staging)


def test_the_harvest_explains_an_empty_staging_tree(tmp_path: Path) -> None:
    with pytest.raises(BuildError, match="produced no UKI"):
        _harvest(tmp_path)


# ---------------------------------------------------------------------------
# The verification
# ---------------------------------------------------------------------------


def _layout_with_uki(tmp_path: Path, cmdline: bytes) -> Layout:
    layout = Layout(tmp_path)
    layout.uki.mkdir(parents=True, exist_ok=True)
    layout.uki_file.write_bytes(make_uki(cmdline))
    return layout


def test_verify_accepts_a_command_line_that_boots_the_iso(tmp_path: Path, console: Console) -> None:
    layout = _layout_with_uki(tmp_path, b"root=live:CDLABEL=UBUNTU-UKI-ISO rd.live.dir=/live")

    _verify(layout, console)


def test_verify_rejects_a_uki_that_would_not_find_the_medium(
    tmp_path: Path, console: Console
) -> None:
    """An installed-style command line builds fine and boots nothing.

    The live image's whole job is to find the squashfs on the medium, so a
    command line pointing at a root filesystem is fatal rather than odd.
    """
    layout = _layout_with_uki(tmp_path, b"root=UUID=1234-abcd ro quiet")

    with pytest.raises(BuildError, match="root=live:"):
        _verify(layout, console)
