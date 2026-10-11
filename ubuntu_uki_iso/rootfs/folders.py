"""Rootfs inputs and outputs under the supplied build directory."""

from pathlib import Path

from ..errors import ConfigError
from ..uki.folders import FolderLayout as KernelLayout


class FolderLayout:
    """
    Resolve kernel inputs, rootfs trees and SquashFS images under one directory.
    """

    def __init__(self, temp_dir: Path) -> None:
        """
        Set the root directory for temporary files and generated artifacts.
        """
        self.root = temp_dir.resolve()

    def kernel_image_deb(self) -> Path:
        """
        Return the unique kernel image deb selected by the recorded kernel release.
        """
        return KernelLayout(self.root).kernel_image_deb()

    def rootfs(self, mode: str) -> Path:
        """
        Return the directory holding a live or installed rootfs tree.
        """
        if mode not in ("live", "installed"):
            raise ConfigError(f"unknown rootfs mode: {mode!r}")
        return self.root / f"rootfs-{mode}"

    def squashfs(self, mode: str) -> Path:
        """
        Return the image path consumed by ISO staging and the live command line.
        """
        if mode == "live":
            return self.root / "iso/live/filesystem.squashfs"
        if mode == "installed":
            return self.root / "iso/payload/rootfs.squashfs"
        raise ConfigError(f"unknown rootfs mode: {mode!r}")

    def require_dirs(self) -> None:
        """
        Create the parent directory for temporary rootfs build trees.
        """
        self.root.mkdir(parents=True, exist_ok=True)
