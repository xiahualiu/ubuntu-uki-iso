"""What the tool needs on the machine it runs on, and what it must never do.

Two separate promises here, and the second is the one that would be easy to
break silently.

The first is that the requirements are *declared*: one file lists the apt
packages, and the tool checks it with ``dpkg-query`` rather than installing
anything. This tool runs on CI runners and in containers it does not own, and a
build step that quietly installs a package is a build step that changes the
environment every other job shares.

The second is that nothing in the package installs a package onto the machine
it runs on. The two exceptions are both inside a chroot of the *target* rootfs,
where the package database belongs to the machine being installed — so this
test pins them by name, and a new call site anywhere else fails.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from ubuntu_uki_iso import proc
from ubuntu_uki_iso.config import host_packages
from ubuntu_uki_iso.errors import ConfigError, PackagesMissing

REPO = Path(__file__).resolve().parents[1]
PACKAGE = REPO / "ubuntu_uki_iso"

#: The only places allowed to install into a package database, and why: both
#: are inside a chroot of the target rootfs being built or installed, never the
#: machine running the tool.
TARGET_ROOTFS_BUILDERS = {
    "ubuntu_uki_iso/build/hooks/common.py",
    "ubuntu_uki_iso/installer/uki.py",
}

#: An apt install that is not a simulation. ``--simulate`` is what makes
#: build/hooks/installed.py's check read-only, and it is exempt.
_APT_INSTALL = re.compile(r'"(?:apt|apt-get)"[\s\S]{0,200}?"install"')
_SIMULATE = re.compile(r'"--simulate"')

#: dpkg -i: the other way to write a package database.
_DPKG_INSTALL = re.compile(r'"dpkg"[\s\S]{0,60}?"-i"')


def _source_files() -> list[Path]:
    return sorted(p for p in PACKAGE.rglob("*.py") if "__pycache__" not in p.parts)


# ---------------------------------------------------------------------------
# The declaration
# ---------------------------------------------------------------------------


def test_the_build_group_is_declared() -> None:
    packages = host_packages("build")
    assert "dracut" in packages
    assert "systemd-ukify" in packages
    assert "dpkg-dev" in packages, "dpkg-deb is how the UKI package is built"


def test_the_test_tools_are_not_demanded_of_a_kernel_build() -> None:
    """A machine building a kernel should not be told it is missing QEMU."""
    assert "qemu-system-x86" not in host_packages("build")
    assert "qemu-system-x86" in host_packages("test")


def test_groups_are_deduplicated_but_keep_their_order() -> None:
    combined = host_packages("build", "test")
    assert len(combined) == len(set(combined))
    assert combined.index("dracut") < combined.index("qemu-system-x86")


def test_an_unknown_group_is_refused_rather_than_ignored() -> None:
    """Silently checking nothing is the failure mode worth refusing."""
    with pytest.raises(ConfigError, match="unknown host package group"):
        host_packages("nonesuch")


def test_the_container_image_can_satisfy_the_declaration() -> None:
    """The Dockerfile is one way to meet the requirement, not its definition.

    Which is only true while it keeps installing everything the file names —
    otherwise the image builds, the tests pass inside it, and a machine that
    installed exactly what the README says is missing something.
    """
    text = (REPO / "build" / "Dockerfile").read_text(encoding="utf-8")
    installed: set[str] = set()
    for block in re.findall(
        r"apt-get install -y --no-install-recommends(.*?)&& rm -rf", text, re.S
    ):
        installed.update(token for token in re.split(r"[\s\\]+", block) if token)

    missing = [name for name in host_packages("build", "test") if name not in installed]
    assert not missing, f"build/Dockerfile does not install: {', '.join(missing)}"


# ---------------------------------------------------------------------------
# The check, not the install
# ---------------------------------------------------------------------------


def test_a_missing_package_is_named_with_the_command_that_fixes_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(proc, "_package_installed", lambda name: name != "dracut")

    with pytest.raises(PackagesMissing) as caught:
        proc.Runner.require_packages("python3", "dracut", "xorriso")

    message = str(caught.value)
    assert "dracut" in message
    assert "apt-get install -y dracut" in message
    assert "python3" not in message.split("Install them with")[0]


def test_every_missing_package_is_reported_at_once(monkeypatch: pytest.MonkeyPatch) -> None:
    """One run, one list. Discovering them one at a time wastes an afternoon."""
    monkeypatch.setattr(proc, "_package_installed", lambda name: False)

    with pytest.raises(PackagesMissing) as caught:
        proc.Runner.require_packages("a", "b", "c")

    assert caught.value.packages == ("a", "b", "c")


def test_nothing_missing_raises_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(proc, "_package_installed", lambda name: True)
    proc.Runner.require_packages("dracut")


# ---------------------------------------------------------------------------
# Nothing is installed onto the machine running this
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("path", _source_files(), ids=lambda p: p.name)
def test_no_module_installs_a_package_outside_a_target_rootfs(path: Path) -> None:
    relative = path.relative_to(REPO).as_posix()
    if relative in TARGET_ROOTFS_BUILDERS:
        pytest.skip("installs into the target rootfs's package database, by design")

    text = path.read_text(encoding="utf-8")

    for match in _APT_INSTALL.finditer(text):
        tail = text[match.start() : match.start() + 400]
        assert _SIMULATE.search(tail), (
            f"{relative} runs an apt install that is not a simulation:\n"
            f"{text[match.start() : match.start() + 120]}"
        )

    assert not _DPKG_INSTALL.search(text), (
        f"{relative} runs dpkg -i. Only the two target-rootfs builders may, and "
        "they are listed in this test by name."
    )
