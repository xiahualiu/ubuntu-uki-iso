"""The build-time proof that the target's one package installs with no network.

The payload carries neither a kernel nor a UKI: the target gets both by
installing the package off the medium, with apt, on a machine that has no
package lists and — during an install — no network. A dependency apt would have
to fetch is therefore a failure that would otherwise appear on the target,
after partitioning, with the disk already formatted and nothing else on the
medium to install.

The chroot is stubbed here. What is being tested is the decision the guard
makes about apt's answer, and the two things around it that would make the
guard itself wrong: the paths apt is given, and whether the staged package is
cleaned up again.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from ubuntu_uki_iso import settings
from ubuntu_uki_iso.build.hooks import installed
from ubuntu_uki_iso.errors import BuildError
from ubuntu_uki_iso.log import Console
from ubuntu_uki_iso.paths import Layout

RELEASE = "7.0.14-ubuntu-uki-iso"
UKI_DEB = f"{settings.PACKAGE_NAME}_{RELEASE}_{settings.DPKG_ARCH}.deb"
STAGING = "tmp/kernel-deb-check"


class _Chroot:
    """Stands in for the chroot helper, and remembers what it was asked to run."""

    def __init__(self, output: bytes, returncode: int = 0) -> None:
        self.output = output
        self.returncode = returncode
        self.argv: list[str] = []

    def __call__(self, root: Path, *argv: str, **kwargs: object) -> subprocess.CompletedProcess:
        self.argv = [str(a) for a in argv]
        return subprocess.CompletedProcess(self.argv, self.returncode, self.output, None)


@pytest.fixture
def target(tmp_path: Path) -> Path:
    path = tmp_path / "rootfs"
    path.mkdir()
    return path


@pytest.fixture
def layout(tmp_path: Path) -> Layout:
    built = Layout(tmp_path)
    built.kernel.mkdir(parents=True, exist_ok=True)
    built.kernel_release_file.write_text(f"{RELEASE}\n", encoding="utf-8")
    built.uki_package.write_bytes(b"")
    return built


def test_a_resolvable_install_passes(
    target: Path, layout: Layout, monkeypatch: pytest.MonkeyPatch, console: Console
) -> None:
    chroot = _Chroot(b"0 upgraded, 1 newly installed, 0 to remove.\n")
    monkeypatch.setattr(installed, "chroot", chroot)

    installed.verify_packages_resolve(target, layout, console)

    # What apt is told to install has to be what was staged: a mismatch here
    # fails on the target, where it is expensive, not here where it is cheap.
    assert f"/{STAGING}/{UKI_DEB}" in chroot.argv
    assert not (target / STAGING).exists()


def test_a_dependency_that_would_be_downloaded_fails_the_build(
    target: Path, layout: Layout, monkeypatch: pytest.MonkeyPatch, console: Console
) -> None:
    """The exact failure this guard exists for.

    apt is happy to plan an install that fetches half the archive. On the
    target there is nothing to fetch it from, so a plan that needs to is a
    failed install — and it would be found after the disk was formatted.
    """
    chroot = _Chroot(
        b"Inst linux-base (4.5 Ubuntu:26.04/resolute [all])\nNeed to get 12.3 MB of archives.\n"
    )
    monkeypatch.setattr(installed, "chroot", chroot)

    with pytest.raises(BuildError, match="cannot be installed"):
        installed.verify_packages_resolve(target, layout, console)

    assert not (target / STAGING).exists()


def test_apt_failing_outright_fails_the_build(
    target: Path, layout: Layout, monkeypatch: pytest.MonkeyPatch, console: Console
) -> None:
    """A missing dependency and a broken apt are both fatal, and both are worth
    failing on: the same command is what runs on the target."""
    chroot = _Chroot(b"E: Unable to locate package linux-base\n", returncode=100)
    monkeypatch.setattr(installed, "chroot", chroot)

    with pytest.raises(BuildError, match="cannot be installed"):
        installed.verify_packages_resolve(target, layout, console)
