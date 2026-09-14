"""Installing the image onto the machine.

The most important module here is :mod:`~ubuntu_uki_iso.installer.preflight`, which
is the part that can destroy 58 TB and is therefore written to be read. The
rest are steps it gates.

The execution model is in :mod:`ubuntu_uki_iso.proc`: nothing mutates except through
:class:`~ubuntu_uki_iso.proc.Runner`, which is what makes dry-run-by-default a
property of the code rather than a promise in a comment.
"""

from __future__ import annotations

from .context import Context
from .preflight import Decision, Preflight, RaidMode

__all__ = ["Context", "Decision", "Preflight", "RaidMode"]
