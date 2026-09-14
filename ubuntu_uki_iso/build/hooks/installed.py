"""mmdebstrap customize hook for the INSTALLED rootfs.

This is the system that ends up on the target disk. The bar is different from
the live hook: nothing that is only useful during installation, no installer,
no autologin, no console conveniences.

Two things are deliberately absent, and they are the same decision.

**There is no kernel here** — not the custom one, not any. The target gets its
kernel by installing the packages the medium carries, with apt, in a chroot,
which is exactly what a kernel upgrade on that machine will be later. Baking a
kernel in would give the machine two ways to have acquired one, and the way
that was not the upgrade path is the way that never gets exercised.

**And there is no UKI**, because the installed command line carries
``root=UUID=@@ROOT_UUID@@``, which is not a real UUID until the installer has
partitioned and formatted the disk. A UKI generated now would bake in the
placeholder and produce an unbootable system that looks fine.

What this hook does have to get right is that the install will work:
:func:`verify_packages_resolve` proves, at build time, that apt can install
those packages from this rootfs alone, with no apt lists and no network.
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

from ... import settings
from ...config import boot, dracut_conf, install_conf
from ...errors import BuildError
from ...log import Console, get_console
from ...paths import Layout
from ...shim import install_kernel_install_plugins, install_kernel_package_hooks
from .common import (
    chroot,
    cleanup,
    install_file,
    install_python_package,
    target_argument,
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


#: Where the packages are put inside the rootfs while apt resolves them. Under
#: /tmp deliberately: that directory is excluded from the squashfs, so a crash
#: between the copy and the cleanup cannot leave them on the medium.
_DEB_CHECK_DIR = Path("tmp/kernel-deb-check")


def verify_packages_resolve(target: Path, layout: Layout, console: Console) -> None:
    """Prove the kernel packages install from this rootfs alone.

    The payload carries no kernel; the target gets one by installing these
    packages from the medium. That puts apt in the position of resolving their
    dependencies on a machine with no apt lists and — during an install — no
    network. A dependency apt would have to fetch is a failure that would
    otherwise appear on the target, after partitioning, with the disk already
    formatted.

    ``--simulate`` resolves the whole thing and changes nothing, so the answer
    is available here for the cost of a few seconds. What is checked is not that
    apt succeeds but that it never asks for the network: ``Need to get`` means
    the install would have needed it.
    """
    image, headers = layout.kernel_debs()
    staging = target / _DEB_CHECK_DIR
    staging.mkdir(parents=True, exist_ok=True)
    for deb in (image, headers):
        shutil.copyfile(deb, staging / deb.name)

    result = chroot(
        target,
        "apt-get",
        "install",
        "--simulate",
        "--no-install-recommends",
        *[f"/{_DEB_CHECK_DIR}/{deb.name}" for deb in (image, headers)],
        check=False,
        capture=True,
    )
    shutil.rmtree(staging, ignore_errors=True)

    output = (result.stdout or b"").decode("utf-8", errors="replace")
    if result.returncode != 0 or "Need to get" in output:
        raise BuildError(
            "the kernel packages cannot be installed from this rootfs alone.\n\n"
            f"apt-get --simulate said:\n{output.strip()}\n\n"
            "The target installs these from the medium with no apt lists and no\n"
            "network, so every dependency must already be in the payload. Add what\n"
            "is missing to data/packages/installed.list. If apt itself is what\n"
            "failed rather than a dependency, that matters more, not less: the\n"
            "same command is what runs on the target."
        )
    console.ok("  the kernel packages resolve offline against this rootfs")


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    console = get_console()

    target = target_argument(argv)
    layout = Layout()

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
    # The plugins only run if something calls kernel-install. On this image that
    # is the postinst hook below and nothing else — see ukis/trigger.py.
    for path in install_kernel_package_hooks(target):
        console.grey(f"  {path.relative_to(target)}")
    console.info(
        "  every kernel install now refreshes \\EFI\\BOOT\\BOOTX64.EFI "
        f"and keeps {settings.UKI_RETENTION} UKIs"
    )

    # --- the kernel packages ----------------------------------------------
    # The kernel reaches the target as a package, so what has to be proven here
    # is that installing it there will work.
    console.step("  hook(installed): kernel packages")
    verify_packages_resolve(target, layout, console)

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
