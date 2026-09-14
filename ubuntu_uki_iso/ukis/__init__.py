"""UKI management: what lives on the ESP and why.

Three concerns, the first two about the EFI System Partition being 1 GB:

:mod:`~ubuntu_uki_iso.ukis.fallback`
    Keeping ``\\EFI\\BOOT\\BOOTX64.EFI`` pointed at the current kernel, so the
    machine boots with no NVRAM configuration and survives losing it.

:mod:`~ubuntu_uki_iso.ukis.retention`
    Pruning ``\\EFI\\Linux`` so kernel upgrades do not fill the ESP.

:mod:`~ubuntu_uki_iso.ukis.trigger`
    Making a kernel *package* install run kernel-install in the first place.
    Without it the other two never run on the installed machine, because
    nothing else on a stock Ubuntu system calls kernel-install.

All three are entry points written onto the target: the first two are
kernel-install plugins in ``/etc/kernel/install.d/``, the third a pair of hooks
in ``/etc/kernel/postinst.d/`` and ``postrm.d``. The logic lives here so it can
be imported and tested; :mod:`ubuntu_uki_iso.shim` generates the executables
that call into it.
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
