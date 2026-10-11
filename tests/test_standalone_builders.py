"""Independent build boundaries, without compiling a kernel or requiring root."""

import subprocess
from pathlib import Path
from unittest.mock import Mock

import pytest

from helpers import make_uki
from ubuntu_uki_iso.errors import BuildError
from ubuntu_uki_iso.kernel import build as kernel
from ubuntu_uki_iso.uki import build as uki


def test_kernel_preflight_failure_precedes_workspace_creation(tmp_path, monkeypatch):
    monkeypatch.setattr(kernel, "preflight", Mock(side_effect=BuildError("missing packages")))
    workspace = tmp_path / "new"
    assert kernel.main(["--temp-dir", str(workspace)]) == 1
    assert not workspace.exists()


def test_kernel_preflight_does_not_require_iso_tools():
    packages = kernel.required_packages()
    assert "build-essential" in packages
    assert "xorriso" not in packages
    assert "dracut" not in packages


@pytest.mark.parametrize("valid", [True, False])
def test_uki_uses_extracted_modules_and_preserves_previous_output(tmp_path, monkeypatch, valid):
    deb = tmp_path / "kernel.deb"
    deb.touch()
    output = tmp_path / "boot.efi"
    output.write_bytes(b"previous")
    monkeypatch.setattr(uki.boot, "cmdline", lambda mode: "root=live:test")
    commands = []

    def run(argv, **kwargs):
        assert kwargs == {"check": True}
        args = [str(a) for a in argv]
        commands.append(args)
        if args[0] == "dpkg-deb":
            tree = Path(args[-1])
            (tree / "boot").mkdir(parents=True)
            (tree / "boot/vmlinuz-7.0-test").touch()
            (tree / "lib/modules/7.0-test").mkdir(parents=True)
        elif args[0] == "dracut":
            Path(args[-1]).write_bytes(b"initramfs")
        elif args[0] == "ukify":
            destination = Path(next(a.split("=", 1)[1] for a in args if a.startswith("--output=")))
            destination.write_bytes(make_uki(b"root=live:test") if valid else make_uki(b"wrong"))
        return subprocess.CompletedProcess(args, 0)

    monkeypatch.setattr(uki.subprocess, "run", run)
    if valid:
        uki.build(deb, output, mode="live")
        assert output.with_suffix(".initramfs.img").read_bytes() == b"initramfs"
    else:
        with pytest.raises(BuildError, match="command line differs"):
            uki.build(deb, output, mode="live")
        assert output.read_bytes() == b"previous"
    dracut = next(c for c in commands if c[0] == "dracut")
    scratch = Path(next(a.split("=", 1)[1] for a in dracut if a.startswith("--tmpdir=")))
    assert scratch.parent == output.parent
    assert "--no-hostonly" in dracut
    assert any(a.startswith("--kmoddir=") and a.endswith("/lib/modules/7.0-test") for a in dracut)
    assert not list(tmp_path.glob("uki-*"))
