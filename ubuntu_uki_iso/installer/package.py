"""
Build and install the live installer's isolated Python environment.
"""

import shutil
import subprocess
import tempfile
from pathlib import Path

from .. import __version__
from ..errors import BuildError

VENV = Path("/opt/ubuntu-uki-installer")
COMMAND = Path("/usr/local/bin/ubuntu-uki-install")


def build_installer_wheel(source: Path, work: Path) -> Path:
    """
    Build a pure-Python, runtime-only installer wheel using host build tools.
    """
    project = work / "source"
    package = project / "ubuntu_uki_iso"
    package.mkdir(parents=True)
    shutil.copytree(
        source / "installer",
        package / "installer",
        ignore=shutil.ignore_patterns("__pycache__", "package.py"),
    )
    (package / "uki").mkdir()
    for name in ("__init__.py", "config.py"):
        shutil.copyfile(source / "uki" / name, package / "uki" / name)
    for name in (
        "__init__.py",
        "settings.py",
        "errors.py",
        "gpt.py",
        "pe.py",
        "media.py",
        "boot_package.py",
    ):
        shutil.copyfile(source / name, package / name)
    (project / "pyproject.toml").write_text(
        '[build-system]\nrequires = ["setuptools>=68", "wheel"]\n'
        'build-backend = "setuptools.build_meta"\n\n'
        '[project]\nname = "ubuntu-uki-installer"\n'
        f'version = "{__version__}"\nrequires-python = ">=3.11"\ndependencies = []\n\n'
        '[project.scripts]\nubuntu-uki-install = "ubuntu_uki_iso.installer.cli:main"\n\n'
        '[tool.setuptools.packages.find]\nwhere = ["."]\ninclude = ["ubuntu_uki_iso*"]\n',
        encoding="utf-8",
    )
    wheels = work / "wheels"
    subprocess.run(
        [
            "/usr/bin/python3",
            "-m",
            "pip",
            "wheel",
            "--no-index",
            "--no-deps",
            "--no-build-isolation",
            "--no-cache-dir",
            "--wheel-dir",
            wheels,
            project,
        ],
        check=True,
    )
    candidates = list(wheels.glob("*.whl"))
    if len(candidates) != 1:
        raise BuildError(f"expected one installer wheel, found {len(candidates)}")
    return candidates[0]


def install(root: Path) -> None:
    """
    Create the venv at its final guest path and install the wheel without network access.
    """
    source = Path(__file__).resolve().parents[1]
    with tempfile.TemporaryDirectory(prefix="installer-wheel-", dir=root.parent) as scratch:
        wheel = build_installer_wheel(source, Path(scratch))
        subprocess.run(["chroot", root, "python3", "-m", "venv", VENV], check=True)
        staging = root / "tmp"
        staging.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="installer-", dir=staging) as target_scratch:
            target_wheel = Path(target_scratch) / wheel.name
            shutil.copyfile(wheel, target_wheel)
            guest_wheel = Path("/") / target_wheel.relative_to(root)
            subprocess.run(
                [
                    "chroot",
                    root,
                    VENV / "bin/python",
                    "-m",
                    "pip",
                    "install",
                    "--no-index",
                    "--no-deps",
                    "--no-cache-dir",
                    "--no-compile",
                    guest_wheel,
                ],
                check=True,
            )
    launcher = root / COMMAND.relative_to("/")
    launcher.parent.mkdir(parents=True, exist_ok=True)
    launcher.symlink_to(VENV / "bin/ubuntu-uki-install")
