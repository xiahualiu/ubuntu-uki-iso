"""UKI management: what lives on the ESP and why.

Two concerns, both about the EFI System Partition being 1 GB:

:mod:`~ubuntu_uki_iso.ukis.fallback`
    Keeping ``\\EFI\\BOOT\\BOOTX64.EFI`` pointed at the current kernel, so the
    machine boots with no NVRAM configuration and survives losing it.

:mod:`~ubuntu_uki_iso.ukis.retention`
    Pruning ``\\EFI\\Linux`` so kernel upgrades do not fill the ESP.

Both are kernel-install plugins — small executables in
``/etc/kernel/install.d/`` that run on every kernel install. The logic lives
here so it can be imported and tested; :mod:`ubuntu_uki_iso.shim` generates the
executables that call into it.
"""

from __future__ import annotations

from .fallback import BACKUP_RELPATH, FALLBACK_RELPATH
from .fallback import install as install_fallback
from .retention import UKI_SUBDIR, find_ukis, prune

__all__ = [
    "BACKUP_RELPATH",
    "FALLBACK_RELPATH",
    "UKI_SUBDIR",
    "find_ukis",
    "install_fallback",
    "prune",
]
