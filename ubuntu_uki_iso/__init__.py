"""
ubuntu-uki-iso — a reproducible Ubuntu image for one specific machine.

A custom kernel trimmed to the hardware actually present, delivered as a UKI
the UEFI firmware boots directly: no GRUB, no boot manager, no shim.

The package has three faces, and the subpackage layout follows them:

:mod:`~ubuntu_uki_iso.build`
    Producing the artifacts — kernel, rootfs images, UKI, squashfs, ISO. Runs
    in a container on the build host.

:mod:`~ubuntu_uki_iso.installer`
    Writing the result onto a machine's storage. Runs in the live environment
    and is the part that can destroy 58 TB, so it defaults to a dry run.

:mod:`~ubuntu_uki_iso.config`
    What the image is made of, and the checks that keep it self-consistent.

Two modules sit underneath all three: :mod:`~ubuntu_uki_iso.proc` is the only path
to running an external command, and :mod:`~ubuntu_uki_iso.log` is the only path to
output. Both exist so that dry-run-by-default is a property of the code rather
than a promise in a comment.
"""

from __future__ import annotations

__version__ = "0.1.0"

__all__ = ["__version__"]
