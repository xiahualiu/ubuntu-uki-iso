"""
VM artifacts under the build directory supplied by the CLI.
"""

from pathlib import Path


class FolderLayout:
    def __init__(self, temp_dir: Path) -> None:
        self.root = temp_dir.resolve()
        self.iso = self.root / "ubuntu_uki_iso.iso"
        self.vm = self.root / "vm"
        self.vm_logs = self.root / "vm-logs"
