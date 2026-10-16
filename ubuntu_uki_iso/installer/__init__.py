"""
Installing the image onto the machine.

The most important module here is :mod:`~ubuntu_uki_iso.installer.preflight`, which
is the part that can destroy 58 TB and is therefore written to be read. The
rest are steps it gates.

The CLI performs read-only planning by default and uses direct subprocess calls. Mutating commands run only
for apply runs after confirmation. Each mutating step also checks the context's
explicit apply and confirmation state before writing.
"""

from __future__ import annotations

from .context import Context
from .preflight import Decision, Preflight, RaidMode

__all__ = ["Context", "Decision", "Preflight", "RaidMode"]
