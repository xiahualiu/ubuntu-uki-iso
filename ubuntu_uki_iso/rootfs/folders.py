"""
Rootfs outputs under the supplied build directory.
"""

from pathlib import Path

from ..errors import ConfigError
from ..settings import Mode


class FolderLayout:
    """
    Resolve boot package inputs, rootfs trees and SquashFS images under one directory.
    """

    def __init__(self, temp_dir: Path) -> None:
        """
        Set the root directory for temporary files and generated artifacts.
        """
        self.root = temp_dir.resolve()

    def rootfs(self, mode: Mode) -> Path:
        """
        Return the directory holding a live or installed rootfs tree.
        """
        if mode not in (Mode.LIVE, Mode.INSTALLED):
            raise ConfigError(f"unknown rootfs mode: {mode!r}")
        return self.root / f"rootfs-{mode}"

    def squashfs(self, mode: Mode) -> Path:
        """
        Return the shared base or live-only layer path consumed by ISO staging.
        """
        if mode == Mode.LIVE:
            return self.root / "iso/live/live-overlay.squashfs"
        if mode == Mode.INSTALLED:
            return self.root / "iso/payload/base.squashfs"
        raise ConfigError(f"unknown rootfs mode: {mode!r}")

    def require_dirs(self) -> None:
        """
        Create the parent directory for temporary rootfs build trees.
        """
        self.root.mkdir(parents=True, exist_ok=True)
