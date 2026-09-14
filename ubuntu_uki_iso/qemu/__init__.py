"""The tests that boot the ISO and drive the installer.

The installer's promises are about a machine: it has to refuse the wrong one,
and it has to leave the right one alone until it is told otherwise. Code that
reads itself cannot check either, so this subpackage builds a machine — the ISO
boots in QEMU against disks that are ordinary files — and reads the result back
out of the guest's own console log.

:func:`run_tests` is the entry point; :mod:`ubuntu_uki_iso.qemu.harness` explains
what each test proves and why the harness is built the way it is.
"""

from __future__ import annotations

from .harness import run_tests

__all__ = ["run_tests"]
