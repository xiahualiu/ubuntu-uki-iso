"""Paths shared by the rootfs, UKI and ISO builders."""

from pathlib import Path


class FolderLayout:
    """Locate prepared inputs and the final ISO under one build directory."""

    def __init__(self, temp_dir: Path) -> None:
        """Resolve the build directory supplied by the caller."""
        self.root = temp_dir.resolve()
        self.live_rootfs = self.root / "iso/live/filesystem.squashfs"
        self.installed_rootfs = self.root / "iso/payload/rootfs.squashfs"
        self.live_uki = self.root / "uki/BOOTX64.EFI"
        self.installed_uki = self.root / "uki/uki.efi"
        self.iso = self.root / "ubuntu_uki_iso.iso"
