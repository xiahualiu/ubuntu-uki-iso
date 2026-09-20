"""What the installer finds on the installation medium.

Two things have to be there for an install to be possible: the rootfs payload,
and the package that carries the kernel and the UKI onto the target. They are
found together, because they arrive together — the ISO's ``/payload``
directory — and the failure to find either has to be loud. An install that
starts without them formats a disk and then has nothing to put on it.
"""

from __future__ import annotations

import io
from pathlib import Path

import pytest

from conftest import RecordingRunner
from ubuntu_uki_iso import paths, settings
from ubuntu_uki_iso.errors import BuildError
from ubuntu_uki_iso.installer import context, target
from ubuntu_uki_iso.installer.context import Context
from ubuntu_uki_iso.installer.preflight import RaidMode
from ubuntu_uki_iso.installer.target import find_payload
from ubuntu_uki_iso.log import Console
from ubuntu_uki_iso.paths import Layout
from ubuntu_uki_iso.proc import Runner

RELEASE = "7.0.14-ubuntu-uki-iso"
UKI_DEB = f"{settings.PACKAGE_NAME}_{RELEASE}_amd64.deb"


def _payload_directory(tmp_path: Path, *, packages: bool = True) -> Path:
    """A directory shaped like the medium's /payload, and the payload in it."""
    payload = tmp_path / "payload"
    (payload / "debs").mkdir(parents=True, exist_ok=True)
    squashfs = payload / paths.PAYLOAD_SQUASHFS.name
    squashfs.write_bytes(b"not really a squashfs")
    if packages:
        (payload / "debs" / UKI_DEB).write_bytes(b"")
    return squashfs


def _context(runner: Runner, console: Console, tmp_path: Path, payload: Path | None) -> Context:
    return Context(
        root_disk="/dev/nvme0n1",
        data_disks=[],
        runner=runner,
        console=console,
        layout=Layout(tmp_path),
        mode=RaidMode.NONE,
        payload=payload,
    )


# ---------------------------------------------------------------------------
# The payload
# ---------------------------------------------------------------------------


def test_a_medium_found_by_volume_label_is_used(
    tmp_path: Path, runner: RecordingRunner, console: Console
) -> None:
    """The fast path: ask the volume label, then ask where it is mounted.

    This is the branch the real install takes, and it depends on two commands
    agreeing — so it is worth a test that they are wired to each other, even
    though only a booted medium can prove blkid answers.
    """
    _payload_directory(tmp_path)
    runner.stub("blkid", stdout="/dev/sda1\n")
    runner.stub("findmnt", stdout=f"{tmp_path}\n")
    ctx = _context(runner, console, tmp_path, payload=None)

    find_payload(ctx)

    assert ctx.payload == tmp_path / paths.PAYLOAD_SQUASHFS


def test_a_payload_that_does_not_exist_is_refused(
    tmp_path: Path, runner: RecordingRunner, console: Console
) -> None:
    ctx = _context(runner, console, tmp_path, payload=tmp_path / "nowhere.squashfs")

    with pytest.raises(BuildError, match="does not exist"):
        find_payload(ctx)


# ---------------------------------------------------------------------------
# The kernel packages
# ---------------------------------------------------------------------------


def test_the_package_is_found_beside_the_payload(
    tmp_path: Path, runner: RecordingRunner, console: Console
) -> None:
    squashfs = _payload_directory(tmp_path)
    ctx = _context(runner, console, tmp_path, payload=squashfs)

    find_payload(ctx)

    assert len(ctx.debs) == 1
    assert ctx.uki_package is not None
    assert ctx.uki_package.name == UKI_DEB


def test_a_medium_without_the_package_is_refused(
    tmp_path: Path, runner: RecordingRunner, console: Console
) -> None:
    """The payload carries no kernel and no UKI, so this is not a detail.

    Without this package the machine gets a root filesystem and nothing to boot:
    the install would have to be refused before it formatted anything.
    """
    squashfs = _payload_directory(tmp_path, packages=False)
    ctx = _context(runner, console, tmp_path, payload=squashfs)

    with pytest.raises(BuildError, match="no packages"):
        find_payload(ctx)


def test_a_dry_run_reports_the_missing_packages_without_dying(tmp_path: Path) -> None:
    """A dry run exists to be run before the machine is near anything.

    Its whole output is a plan, so it has to survive what an apply run must
    refuse — while still saying so.
    """
    err = io.StringIO()
    console = Console(colour=False, stdout=io.StringIO(), stderr=err)
    runner = Runner(dry_run=True, console=console)
    squashfs = _payload_directory(tmp_path, packages=False)
    ctx = _context(runner, console, tmp_path, payload=squashfs)

    find_payload(ctx)

    assert ctx.debs == []
    assert "no packages" in err.getvalue()


def test_the_volume_label_is_the_one_the_iso_is_burned_with(
    tmp_path: Path, runner: RecordingRunner, console: Console
) -> None:
    """A label that drifts from settings is a medium that is never found."""
    _payload_directory(tmp_path)
    runner.stub("blkid", stdout="/dev/sda1\n")
    runner.stub("findmnt", stdout=f"{tmp_path}\n")
    find_payload(_context(runner, console, tmp_path, payload=None))

    assert ["blkid", "-L", settings.VOLID] in runner.probed


# ---------------------------------------------------------------------------
# The fstab
# ---------------------------------------------------------------------------


def test_the_fstab_mounts_the_esp_where_kernel_install_looks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The invariant that ties the installed system to its boot root.

    Two things are written at different times and have to agree: the fstab
    mounts the ESP, and the UKI package's postinst writes to that same
    directory — both from ``settings.ESP_MOUNT``. If they drift apart the UKI
    is written somewhere the firmware never looks, and nothing says so until
    the machine is rebooted.
    """
    # Context.target is the real mount point the installer uses, which no test
    # may create. Read at call time, so it can be pointed at tmp_path.
    monkeypatch.setattr(context, "TARGET_MOUNT", tmp_path)
    console = Console(colour=False, stdout=io.StringIO(), stderr=io.StringIO())
    runner = Runner(dry_run=True, console=console)
    ctx = _context(runner, console, tmp_path, payload=None)
    ctx.root_uuid = "11111111-2222-3333-4444-555555555555"
    ctx.esp_uuid = "66666666-7777-8888-9999-000000000000"
    # In production the unsquashed payload supplies /etc, which the step checks
    # for before getting this far.
    (tmp_path / "etc").mkdir(parents=True)

    target._write_fstab(ctx)

    fstab = (ctx.target / "etc/fstab").read_text()
    assert f" {settings.ESP_MOUNT} vfat" in fstab
    assert (ctx.target / settings.ESP_MOUNT.lstrip("/")).is_dir()
