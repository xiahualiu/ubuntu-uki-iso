"""mmdebstrap customize hook for the LIVE rootfs.

The live rootfs is the installer's operating system. It is allowed to be
comfortable — parted, mdadm, an editor, a shell — because none of it reaches
the target disk.

The one thing it must get right is the UKI: because ``/etc/kernel/cmdline``
here holds the *live* command line, running kernel-install inside this rootfs
produces exactly the UKI the ISO needs. The command line is system state, not a
build-time argument, which is the whole point of the design.
"""

from __future__ import annotations

import sys

from ...config import boot, dracut_conf, install_conf
from ...log import get_console
from ...paths import Layout
from .common import (
    chroot,
    cleanup,
    install_debs,
    install_file,
    install_python_package,
    target_argument,
    verify_kernel_installed,
    write_file,
)

#: Serial console autologin. This is what makes ``qemu -serial stdio`` land at
#: a shell, which is the live-boot proof in the test suite.
_AUTOLOGIN = """\
# Live installer environment: land at a root shell on the console.
[Service]
ExecStart=
ExecStart=-/sbin/agetty --autologin root --noclear %I $TERM
"""

_DHCP_NETWORK = """\
[Match]
Type=ether

[Network]
DHCP=yes
"""


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    console = get_console()

    target = target_argument(argv)
    layout = Layout()
    release = layout.kernel_release()

    console.step(f"  hook(live): install kernel {release}")
    install_debs(target, list(layout.kernel_debs()))
    verify_kernel_installed(target, release)
    console.info(f"  kernel {release} installed")

    # --- boot configuration -----------------------------------------------
    console.step("  hook(live): boot configuration")
    install_file(install_conf(), target / "etc/kernel/install.conf")

    # The rendered live cmdline, not the template: the volume label has to be
    # concrete here, because the ISO is built with that exact label and dracut
    # will search for it by name.
    write_file(target / "etc/kernel/cmdline", boot.cmdline_live() + "\n")
    install_file(dracut_conf("live"), target / "etc/dracut.conf.d/00-live.conf")
    console.info(f"  cmdline: {boot.cmdline_live()}")

    # --- the installer ----------------------------------------------------
    # Copied from the live rootfs's own copy of the package, so there is one
    # implementation of the RAID safety logic in the world and no way for the
    # ISO's version to drift from the reviewed one.
    console.step("  hook(live): the installer")
    install_python_package(target, console)

    # --- console and network ----------------------------------------------
    console.step("  hook(live): console and network")
    for unit in ("serial-getty@ttyS0.service", "getty@tty1.service"):
        write_file(target / f"etc/systemd/system/{unit}.d/autologin.conf", _AUTOLOGIN)

    write_file(target / "etc/systemd/network/20-wired.network", _DHCP_NETWORK)

    for unit in (
        "systemd-networkd.service",
        "systemd-resolved.service",
        "serial-getty@ttyS0.service",
    ):
        chroot(target, "systemctl", "enable", unit, check=False, quiet=True)

    # Root's password stays locked. Autologin on the console is a convenience
    # for whoever is standing at the machine; there is no sshd in the live
    # image, so it is not a network-exposed credential.
    chroot(target, "passwd", "-l", "root", check=False, quiet=True)

    # --- trim -------------------------------------------------------------
    console.step("  hook(live): cleanup")
    cleanup(target)
    console.info("  hook(live): done")
    return 0


if __name__ == "__main__":
    sys.exit(main())
