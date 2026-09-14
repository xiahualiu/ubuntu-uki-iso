"""Where things live.

Two different roots, and conflating them is a bug:

*The data root* ships with the package — Kconfig fragments, package lists,
cmdline templates. It is inside the installed package and is read-only.

*The workspace root* is the checkout being built. All output goes under
``<workspace>/out``. On a machine that only installs the wheel, the workspace
is wherever the user runs the command, found by walking up for a pyproject.toml
or by ``UBUNTU_UKI_ISO_ROOT``.
"""

from __future__ import annotations

import os
from pathlib import Path

# ---------------------------------------------------------------------------
# The installation medium's own layout
# ---------------------------------------------------------------------------

#: One spelling for the paths that the build writes into the ISO and the
#: installer looks for on the medium. Two modules have to agree on every one of
#: them, and a disagreement is an ISO that boots, installs nothing, and says so
#: only after the target disk has been formatted.
LIVE_SQUASHFS = Path("live/filesystem.squashfs")
PAYLOAD_SQUASHFS = Path("payload/rootfs.squashfs")

#: The kernel packages the installer installs on the target, beside the payload
#: rather than inside it: the payload is unsquashed verbatim, while these are
#: handed to the package manager on the target. That is what makes a future
#: kernel update there the same operation as the install was.
PAYLOAD_DEBS = Path("payload/debs")


def data_dir() -> Path:
    """Packaged configuration data (fragments, lists, templates)."""
    return Path(__file__).resolve().parent / "data"


def data_file(*parts: str) -> Path:
    """A file under the data directory, checked to exist.

    Checked here rather than at the point of use because a missing data file
    means the wheel was built wrong, and an obscure failure thirty minutes into
    a kernel build is the expensive way to find that out.
    """
    path = data_dir().joinpath(*parts)
    if not path.exists():
        from .errors import ConfigError

        raise ConfigError(
            f"packaged data file is missing: {path}\n"
            "This means the installation is incomplete — the data files are "
            "part of the package. Reinstall, or run from a checkout."
        )
    return path


def workspace_root(start: Path | None = None) -> Path:
    """The checkout being built.

    ``UBUNTU_UKI_ISO_ROOT`` wins; otherwise walk up from ``start`` (or the working
    directory) looking for a pyproject.toml. Falls back to the working
    directory, which is the right answer for a wheel installed on a machine
    that has no checkout.
    """
    env = os.environ.get("UBUNTU_UKI_ISO_ROOT")
    if env:
        return Path(env).resolve()

    here = (start or Path.cwd()).resolve()
    for candidate in (here, *here.parents):
        if (candidate / "pyproject.toml").is_file():
            return candidate
    return here


class Layout:
    """Output paths, all derived from one root so ``clean`` means something."""

    def __init__(self, root: Path | None = None) -> None:
        self.root = (root or workspace_root()).resolve()

    @property
    def out(self) -> Path:
        return self.root / "out"

    # -- kernel ------------------------------------------------------------

    @property
    def kernel(self) -> Path:
        return self.out / "kernel"

    @property
    def kernel_release_file(self) -> Path:
        """The single authority on the built kernel's release string.

        ``linux-source-7.0.0`` carries upstream 7.0.14, so the package name is
        not the release and nothing may infer one. Written by the kernel build,
        read by everything downstream.
        """
        return self.kernel / "release"

    def kernel_debs(self) -> tuple[Path, Path]:
        """The kernel image and headers packages, as built.

        Both go to the target: the image carries the kernel and its modules,
        and the headers match it exactly, which Docker and any future DKMS
        module need. Deliberately absent is the ``-dbg`` package — hundreds of
        megabytes that nothing here consumes.

        One implementation, because three callers need the same pair: the live
        rootfs installs them, the installed rootfs checks that they resolve, and
        the ISO carries them for the target to install.
        """
        from .errors import BuildError

        found = []
        for pattern, what in (
            ("linux-image-*.deb", "image"),
            ("linux-headers-*.deb", "headers"),
        ):
            candidates = [p for p in sorted(self.kernel.glob(pattern)) if "-dbg_" not in p.name]
            if not candidates:
                raise BuildError(
                    f"no kernel {what} .deb in {self.kernel}.\n"
                    "Run `ubuntu-uki-iso build kernel` first."
                )
            found.append(candidates[-1])
        return found[0], found[1]

    # -- rootfs ------------------------------------------------------------

    @property
    def rootfs_live(self) -> Path:
        return self.out / "rootfs-live"

    @property
    def rootfs_installed(self) -> Path:
        """The system that gets written to the target disk.

        It travels on the ISO as the installer's *payload*; the two words
        describe one artifact from two directions.
        """
        return self.out / "rootfs-installed"

    # -- image -------------------------------------------------------------

    @property
    def uki(self) -> Path:
        return self.out / "uki"

    @property
    def uki_file(self) -> Path:
        return self.uki / "BOOTX64.EFI"

    @property
    def iso_stage(self) -> Path:
        return self.out / "iso"

    @property
    def iso(self) -> Path:
        return self.out / "ubuntu_uki_iso.iso"

    # -- test --------------------------------------------------------------

    @property
    def vm(self) -> Path:
        return self.out / "vm"

    @property
    def vm_logs(self) -> Path:
        return self.out / "vm-logs"

    # -- scratch -----------------------------------------------------------

    @property
    def work(self) -> Path:
        return self.out / "work"

    def kernel_release(self) -> str:
        """Read the built kernel's release, or explain what to run first."""
        from .errors import BuildError

        path = self.kernel_release_file
        if not path.is_file() or not path.read_text().strip():
            raise BuildError(
                f"the kernel release has not been recorded ({path} is missing).\n"
                "Run `ubuntu-uki-iso build kernel` first."
            )
        return path.read_text().strip()

    def require_dirs(self) -> None:
        for path in (self.kernel, self.work, self.iso_stage):
            path.mkdir(parents=True, exist_ok=True)


def default_layout() -> Layout:
    return Layout()
