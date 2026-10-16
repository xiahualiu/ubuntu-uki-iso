"""
Paths for the standalone UKI build, beside the kernel build outputs.
"""

from pathlib import Path

from ..errors import BuildError


class FolderLayout:
    """
    Resolve kernel inputs and UKI outputs under the supplied directory.
    """

    def __init__(self, temp_dir: Path) -> None:
        """
        Set the root directory for kernel inputs and UKI build files.
        """
        self.root = temp_dir.resolve()

    @property
    def kernel(self) -> Path:
        """
        Directory holding kernel packages and release metadata.
        """
        return self.root / "kernel"

    @property
    def uki(self) -> Path:
        """
        Directory holding the generated EFI image and initramfs.
        """
        return self.root / "uki"

    def kernel_image_deb(self) -> Path:
        """
        Select the kernel image package matching the recorded release.

        Returns:
            Path to the unique image package. Raises BuildError for missing or
            ambiguous inputs. Without release metadata, requires one image package.
        """
        release_file = self.kernel / "release"
        release = release_file.read_text().strip() if release_file.is_file() else None
        candidates = [
            path
            for path in sorted(self.kernel.glob("linux-image-*.deb"))
            if "-dbg_" not in path.name
            and (release is None or path.name.partition("_")[0] == f"linux-image-{release}")
        ]
        if len(candidates) != 1:
            raise BuildError(
                f"expected one kernel image deb in {self.kernel}, found {len(candidates)}; "
                "run the kernel build first or supply --kernel-deb"
            )
        return candidates[0]

    def require_dirs(self) -> None:
        """
        Create the UKI output directory when it is missing.
        """
        self.uki.mkdir(parents=True, exist_ok=True)
