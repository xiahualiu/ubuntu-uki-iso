"""Validate the rootfs and UKI artifacts carried by installation media."""

from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

from . import pe
from .errors import BuildError
from .uki.config import cmdline


def digest(path: Path) -> str:
    """Return a file's SHA256 without loading the whole artifact into memory."""
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def validate_uki(path: Path, mode: str) -> str:
    """Check a UKI's boot sections and command line and return its kernel release."""
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


def validate_rootfs(path: Path, release: str, mode: str) -> None:
    """Check SquashFS release metadata, boot parameters and matching runtime modules."""
    for relative, expected in (
        ("usr/lib/ubuntu-uki-iso/release", release),
        ("etc/kernel/cmdline", cmdline(mode)),
    ):
        result = subprocess.run(
            ["unsquashfs", "-cat", path, relative], check=True, capture_output=True, text=True
        )
        if result.stdout.strip() != expected:
            raise BuildError(f"{path}: {relative} does not match the {mode} UKI")
    result = subprocess.run(
        ["unsquashfs", "-ll", path, f"usr/lib/modules/{release}"],
        check=True,
        capture_output=True,
        text=True,
    )
    if f"squashfs-root/usr/lib/modules/{release}/" not in result.stdout:
        raise BuildError(f"{path} has no runtime modules for {release}")


def validate_payload(directory: Path) -> str:
    """Validate installed artifacts and manifest before allowing disk writes."""
    rootfs, uki = directory / "rootfs.squashfs", directory / "uki.efi"
    try:
        manifest = json.loads((directory / "manifest.json").read_text())
        release = validate_uki(uki, "installed")
        if manifest["version"] != 1 or manifest["release"] != release:
            raise BuildError("payload manifest has an incompatible version or kernel release")
        for path in (rootfs, uki):
            if digest(path) != manifest["sha256"][path.name]:
                raise BuildError(f"payload checksum mismatch: {path.name}")
        validate_rootfs(rootfs, release, "installed")
        return release
    except (OSError, ValueError, KeyError, TypeError, subprocess.CalledProcessError) as exc:
        raise BuildError(f"invalid installation payload in {directory}: {exc}") from exc


def read_kernel_hash(path: Path) -> str:
    """Return the embedded kernel hash for comparing live and installed UKIs."""
    raw = pe.read_section(path, ".linux")
    if not raw:
        raise BuildError(f"{path} has no kernel")
    return hashlib.sha256(raw).hexdigest()
