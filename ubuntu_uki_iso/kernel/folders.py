"""
Paths owned by the kernel build submodule.
"""

from __future__ import annotations

from pathlib import Path


class FolderLayout:
    """
    Resolve kernel outputs and temporary paths under the supplied directory.
    """

    def __init__(self, temp_dir: Path) -> None:
        """
        Set the root directory for all kernel files.
        """
        self.root: Path = temp_dir.resolve()

    @property
    def kernel(self) -> Path:
        """
        Directory holding kernel packages, configs, release metadata, and logs.
        """
        return self.root / "kernel"

    @property
    def download_dir(self) -> Path:
        """
        Directory for downloaded source packages and extracted kernel trees.
        """
        return self.root / "src"

    def require_dirs(self) -> None:
        """
        Create the output and temporary directories when they are missing.
        """
        for path in (self.kernel, self.root):
            path.mkdir(parents=True, exist_ok=True)
