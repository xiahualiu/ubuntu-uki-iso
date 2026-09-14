"""mmdebstrap customize hook for the INSTALLED rootfs.

This is the system that ends up on the target disk. The bar is different from
the live hook: nothing that is only useful during installation, no installer,
no autologin, no console conveniences.

The one thing worth reading carefully is what is *not* done here. No UKI is
generated, because the installed command line carries
``root=UUID=@@ROOT_UUID@@`` and that is not a real UUID until the installer has
partitioned and formatted the disk. Generating one now would bake in a
placeholder and produce an unbootable system that looks fine. The installer
substitutes the real UUID and runs kernel-install for real, on the target,
after the filesystem exists.
"""

from __future__ import annotations

import sys

from ... import settings
from ...config import boot, dracut_conf, install_conf
from ...log import get_console
from ...paths import Layout
from ...shim import install_kernel_install_plugins
from .common import (
    chroot,
    cleanup,
    headers_deb,
    image_deb,
    install_debs,
    install_file,
    install_python_package,
    target_argument,
    verify_kernel_installed,
    write_file,
)

_DHCP_NETWORK = """\
[Match]
Type=ether

[Network]
DHCP=yes
"""

#: Records the kernel release that reached multi-user.
#:
#: ``ubuntu-uki-iso install --bless`` refuses to repoint the firmware fallback until
#: this file exists and names the kernel that is running — which is what stops
#: a UKI that has never booted from replacing the one that works.
_MARK_BOOTED = """\
[Unit]
Description=Record that this kernel booted successfully
Documentation=man:ubuntu-uki-iso(1)
After=multi-user.target

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/usr/local/bin/ubuntu-uki-iso mark-booted

[Install]
WantedBy=multi-user.target
"""

_UNITS = (
    "systemd-networkd.service",
    "systemd-resolved.service",
    "docker.service",
    "ssh.service",
    "mdmonitor.service",
    "xiahualab-mark-booted.service",
)


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    console = get_console()

    target = target_argument(argv)
    layout = Layout()
    release = layout.kernel_release()

    console.step(f"  hook(installed): install kernel {release}")
    install_debs(target, [image_deb(layout), headers_deb(layout)])
    verify_kernel_installed(target, release)
    console.info(f"  kernel {release} installed")

    # --- configuration ----------------------------------------------------
    console.step("  hook(installed): configuration")
    install_file(install_conf(), target / "etc/kernel/install.conf")
    install_file(dracut_conf("installed"), target / "etc/dracut.conf.d/10-installed.conf")

    # The unrendered template, deliberately. The installer substitutes the real
    # root UUID and verifies that no placeholder survives.
    write_file(target / "etc/kernel/cmdline", boot.cmdline_installed_unrendered() + "\n")

    write_file(target / "etc/hostname", f"{settings.BOOT_LABEL}\n")
    write_file(
        target / "etc/hosts",
        f"127.0.0.1\tlocalhost\n127.0.1.1\t{settings.BOOT_LABEL}\n",
    )

    # --- the package, and the kernel-install plugins ----------------------
    console.step("  hook(installed): plugins")
    install_python_package(target, console)

    for path in install_kernel_install_plugins(target):
        console.grey(f"  {path.relative_to(target)}")
    console.info(
        "  every kernel install now refreshes \\EFI\\BOOT\\BOOTX64.EFI "
        f"and keeps {settings.UKI_RETENTION} UKIs"
    )

    # --- services ---------------------------------------------------------
    console.step("  hook(installed): services")
    write_file(target / "etc/systemd/network/20-wired.network", _DHCP_NETWORK)
    write_file(
        target / "etc/systemd/system/xiahualab-mark-booted.service",
        _MARK_BOOTED,
    )

    for unit in _UNITS:
        chroot(target, "systemctl", "enable", unit, check=False, quiet=True)

    # No swap of any kind is configured: no swapfile, no swap partition, no
    # zram. With no swap there is also no hibernation, which is a property of
    # the design rather than something untested.

    # --- trim -------------------------------------------------------------
    console.step("  hook(installed): cleanup")
    cleanup(target)

    # machine-id is emptied so the installed system generates its own on first
    # boot. It must not be shared with the live environment: systemd uses it to
    # key journald's persistent storage and the UKI filename.
    machine_id = target / "etc/machine-id"
    if machine_id.exists():
        machine_id.unlink()

    console.info("  hook(installed): done")
    return 0


if __name__ == "__main__":
    sys.exit(main())
