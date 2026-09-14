"""The hook that makes installing a kernel package build a UKI.

The kernel packages this project builds come from ``make bindeb-pkg``, and their
maintainer scripts do exactly one thing: ``run-parts`` over
``/etc/kernel/postinst.d`` and ``/etc/kernel/postrm.d``. They do not call
``kernel-install``. Neither does anything in a stock Ubuntu system — the package
that would, ``systemd-boot``, is deliberately not installed, because this design
has no bootloader.

So without this hook, installing the kernel package produces a machine with a
kernel, an initramfs in ``/boot``, and no UKI at all: the firmware goes on
booting whatever UKI was there before, and on a fresh install that is nothing.
The hook is what closes the loop — and it is the same hook on the install and on
every upgrade after it, which is the entire reason the kernel arrives as a
package.

``run-parts`` passes each hook the kernel release and the path of the installed
image, which is what ``kernel-install add`` wants. It passes the same two
arguments on removal.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys

from .. import settings

#: Part of systemd, which every image here has.
KERNEL_INSTALL = "kernel-install"


def postinst_main(argv: list[str] | None = None) -> int:
    """Build the UKI. Called after a kernel image is installed."""
    return _kernel_install("add", sys.argv if argv is None else argv)


def postrm_main(argv: list[str] | None = None) -> int:
    """Remove the UKI. Called after a kernel image is removed."""
    return _kernel_install("remove", sys.argv if argv is None else argv)


def _kernel_install(verb: str, argv: list[str]) -> int:
    """``argv`` is the hook's own, so ``argv[1]`` is the release."""
    args = [arg for arg in argv[1:] if arg]
    kernel_install = shutil.which(KERNEL_INSTALL)

    # Nothing to do, and not worth failing a kernel install over: run-parts
    # always passes the release, and kernel-install is present on any image this
    # project builds. Both checks exist so that a hook which somehow runs in the
    # wrong place exits quietly rather than breaking dpkg.
    if not args or kernel_install is None:
        return 0

    command = [kernel_install, verb, args[0]]
    if verb == "add":
        if len(args) < 2:
            return 0
        command.append(args[1])

    # The boot root is the ESP, and it is set rather than autodetected: on a
    # machine that has more than one ESP, autodetection is a guess, and the
    # guess decides whether the machine can boot.
    #
    # The exit status is passed through deliberately. A UKI that fails to build
    # is worth failing a kernel install over — the alternative is a machine that
    # silently goes on booting the old kernel.
    environment = dict(os.environ, KERNEL_INSTALL_BOOT_ROOT=str(settings.ESP_MOUNT))
    return subprocess.call(command, env=environment)


if __name__ == "__main__":
    sys.exit(postinst_main())
