"""
Exercise the installer wheel in an actual isolated Python environment.
"""

import os
import subprocess
import sys
import zipfile
from pathlib import Path

from ubuntu_uki_iso.installer.package import build_installer_wheel


def test_installer_wheel_runs_without_checkout_or_build_modules(tmp_path: Path) -> None:
    source = Path(__file__).resolve().parents[1] / "ubuntu_uki_iso"
    wheel = build_installer_wheel(source, tmp_path / "build")
    with zipfile.ZipFile(wheel) as archive:
        names = archive.namelist()
        assert "ubuntu_uki_iso/installer/cli.py" in names
        assert "ubuntu_uki_iso/uki/config.py" in names
        assert not any(
            name.startswith(
                (
                    "ubuntu_uki_iso/kernel/",
                    "ubuntu_uki_iso/build/",
                    "ubuntu_uki_iso/rootfs/",
                    "ubuntu_uki_iso/qemu/",
                )
            )
            for name in names
        )
        assert "ubuntu_uki_iso/uki/build.py" not in names
        assert "ubuntu_uki_iso/installer/package.py" not in names
        assert "ubuntu_uki_iso/cli.py" not in names
        assert "ubuntu_uki_iso/proc.py" not in names
    environment = os.environ.copy()
    environment.pop("PYTHONPATH", None)
    venv = tmp_path / "venv"
    subprocess.run([sys.executable, "-m", "venv", venv], check=True, env=environment)
    subprocess.run(
        [venv / "bin/python", "-m", "pip", "install", "--no-index", "--no-deps", wheel],
        check=True,
        cwd=tmp_path,
        env=environment,
    )
    result = subprocess.run(
        [venv / "bin/ubuntu-uki-install", "--help"],
        check=True,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=environment,
    )
    assert "--root-disk" in result.stdout
    assert "--payload" in result.stdout
    assert (venv / "bin/ubuntu-uki-install").read_text().splitlines()[0] == f"#!{venv}/bin/python"
