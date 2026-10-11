"""Where things live.

Two different roots, and conflating them is a bug:

*The configuration* is Python code inside the installed package. Native tool
configuration files are rendered into the workspace when needed.

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

    # -- image -------------------------------------------------------------

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
