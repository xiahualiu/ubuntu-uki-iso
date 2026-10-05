"""Independent build boundaries, without compiling a kernel or requiring root."""

from pathlib import Path
from unittest.mock import Mock

import pytest

from helpers import make_uki
from ubuntu_uki_iso.errors import BuildError, PackagesMissing
from ubuntu_uki_iso.kernel import build as kernel
from ubuntu_uki_iso.proc import Result
from ubuntu_uki_iso.uki import builder as uki


def test_kernel_preflight_failure_precedes_workspace_creation(tmp_path, monkeypatch):
    monkeypatch.setattr(
        kernel.Runner, "require_packages", Mock(side_effect=PackagesMissing(["build-essential"]))
    )
    workspace = tmp_path / "new"
    assert kernel.main(["--workspace", str(workspace)]) == 1
    assert not workspace.exists()


def test_kernel_preflight_does_not_require_iso_tools():
    runner = Mock()
    kernel.preflight(runner)
    packages = runner.require_packages.call_args.args
    assert "build-essential" in packages
    assert "xorriso" not in packages
    assert "dracut" not in packages


@pytest.mark.parametrize("valid", [True, False])
def test_uki_uses_extracted_modules_and_preserves_previous_output(tmp_path, valid):
    deb = tmp_path / "kernel.deb"
    deb.touch()
    output = tmp_path / "boot.efi"
    output.write_bytes(b"previous")
    runner = Mock()
    commands = []

    def run(*argv):
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
        return Result(args, 0)

    runner.run.side_effect = run
    if valid:
        uki.build(deb, output, runner, cmdline="root=live:test")
        assert output.with_suffix(".initramfs.img").read_bytes() == b"initramfs"
    else:
        with pytest.raises(BuildError, match="command line differs"):
            uki.build(deb, output, runner, cmdline="root=live:test")
        assert output.read_bytes() == b"previous"
    dracut = next(c for c in commands if c[0] == "dracut")
    assert "--no-hostonly" in dracut
    assert any(a.startswith("--kmoddir=") and a.endswith("/lib/modules/7.0-test") for a in dracut)
    assert not list(tmp_path.glob("uki-*"))
