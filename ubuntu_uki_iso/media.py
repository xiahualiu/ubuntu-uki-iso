"""
Validate the rootfs and UKI artifacts carried by installation media.
"""

from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path

from . import pe
from .errors import BuildError
from .settings import Mode
from .uki.config import cmdline


def digest(path: Path) -> str:
    """
    Return a file's SHA256 without loading the whole artifact into memory.
    """
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def validate_uki(path: Path, mode: Mode) -> str:
    """
    Check a UKI's boot sections and command line and return its kernel release.
    """
    try:
        for section in (".linux", ".initrd", ".uname"):
            if not pe.read_section(path, section):
                raise BuildError(f"{path} has no {section} content")
        if pe.cmdline(path) != cmdline(mode):
            raise BuildError(f"{path} has the wrong {mode} command line")
        raw = pe.read_section(path, ".uname")
        release = raw.decode().strip("\0\n ") if raw else ""
    except (pe.NotPE, UnicodeError) as exc:
        raise BuildError(f"invalid UKI {path}: {exc}") from exc
    if not release or "/" in release or release in (".", ".."):
        raise BuildError(f"invalid kernel release in {path}")
    return release


def validate_rootfs(path: Path, release: str, mode: Mode) -> None:
    """
    Check release and boot parameters, plus runtime modules in the shared base.
    """
    for relative, expected in (
        ("usr/lib/ubuntu-uki-iso/release", release),
        ("etc/kernel/cmdline", cmdline(mode)),
    ):
        result = subprocess.run(
            ["unsquashfs", "-cat", path, relative], check=True, capture_output=True, text=True
        )
        if result.stdout.strip() != expected:
            raise BuildError(f"{path}: {relative} does not match the {mode} UKI")
    if mode == Mode.LIVE:
        return
    result = subprocess.run(
        ["unsquashfs", "-ll", path, f"usr/lib/modules/{release}"],
        check=True,
        capture_output=True,
        text=True,
    )
    if f"squashfs-root/usr/lib/modules/{release}/" not in result.stdout:
        raise BuildError(f"{path} has no runtime modules for {release}")
