"""
Live layer differences preserve metadata and hide deleted base files.
"""

import os
import stat

from ubuntu_uki_iso.rootfs.layers import difference
from ubuntu_uki_iso.uki.layers import HOOK


def test_difference_contains_changes_and_whiteouts_without_duplicating_base(tmp_path, monkeypatch):
    base, live, layer = (tmp_path / name for name in ("base", "live", "layer"))
    for root in (base, live):
        (root / "etc").mkdir(parents=True)
        (root / "etc/same").write_text("shared")
        (root / "etc/changed").write_text("base" if root == base else "live")
        (root / "etc/mode").write_text("same contents")
        (root / "etc/link").symlink_to("base" if root == base else "live")
    (live / "etc/mode").chmod(0o600)
    (base / "etc/deleted").write_text("must be hidden")
    (live / "etc/new").write_text("installer")
    whiteouts = []

    def mknod(path, mode, device):
        whiteouts.append((path.relative_to(layer), mode, device))
        path.touch()

    monkeypatch.setattr(os, "mknod", mknod)
    difference(base, live, layer)
    assert not (layer / "etc/same").exists()
    assert (layer / "etc/changed").read_text() == "live"
    assert (layer / "etc/new").read_text() == "installer"
    assert (layer / "etc/link").readlink().as_posix() == "live"
    assert (layer / "etc/mode").stat().st_mode & 0o777 == 0o600
    assert len(whiteouts) == 1
    path, mode, device = whiteouts[0]
    assert str(path) == "etc/deleted" and stat.S_ISCHR(mode) and device == os.makedev(0, 0)


def test_live_hook_uses_readonly_layers_and_fails_closed():
    import subprocess

    subprocess.run(["sh", "-n"], input=HOOK, text=True, check=True)
    assert "ro,lowerdir=/run/initramfs/installer:/run/initramfs/shared-base" in HOOK
    assert "die" in HOOK and "return 1" in HOOK
