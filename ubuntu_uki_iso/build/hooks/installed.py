"""mmdebstrap customize hook for the INSTALLED rootfs.

This is the system that ends up on the target disk. The bar is different from
the live hook: nothing that is only useful during installation, no installer,
no autologin, no console conveniences.

**There is no kernel here, and no UKI**, because both arrive together in the
package the installer installs — built on the build machine, not here and not
on the target. That is the shape of the whole design: the machine that boots
never builds its own boot artifact, so its first install and every later kernel
update are the same operation, installing a version of that package, rather
than two mechanisms of which only one is ever exercised again.

Nor is there anything here that *could* build one. The kernel-install plugins
and the hooks that would call kernel-install are deliberately not installed:
the package's own postinst does the work, and a kernel-install run on this
machine would race it.

What this hook does have to get right is that the install will work:
:func:`verify_packages_resolve` proves, at build time, that apt can install the
package from this rootfs alone, with no apt lists and no network.
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
    """Prove the UKI package installs from this rootfs alone.

    The payload carries no kernel and no UKI; the target gets both by installing
    this one package from the medium. That puts apt in the position of
    resolving its dependencies on a machine with no apt lists and — during an
    install — no network. A dependency apt would have to fetch is a failure
    that would otherwise appear on the target, after partitioning, with the
    disk already formatted, and with nothing else on the medium to install.

    ``--simulate`` resolves the whole thing and changes nothing, so the answer
    is available here for the cost of a few seconds. What is checked is not that
    apt succeeds but that it never asks for the network: ``Need to get`` means
    the install would have needed it.
    """
    package = layout.uki_package
    if not package.is_file():
        raise BuildError(
            f"no UKI package at {package}.\n"
            "Build it first (`ubuntu-uki-iso build uki-target`): the installed image\n"
            "can only prove an install that has an artifact to install."
        )

    staging = target / _DEB_CHECK_DIR
    staging.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(package, staging / package.name)

    result = chroot(
        target,
        "apt-get",
        "install",
        "--simulate",
        "--no-install-recommends",
        f"/{_DEB_CHECK_DIR}/{package.name}",
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

    # The rendered command line, not a template: it names a fixed PARTUUID that
    # the installer writes into the partition table, so there is nothing left
    # to substitute on the target. The installer checks it still matches rather
    # than rewriting it — see installer/target.py.
    write_file(target / "etc/kernel/cmdline", boot.cmdline_installed() + "\n")

    # Without this, a UKI built on the target would be named after the
    # machine-id, which the target does not have until it first boots — and
    # which differs from the build machine's. A fixed token is what lets the
    # prebuilt UKI's name be the one every later build would choose too.
    write_file(target / "etc/kernel/entry-token", f"{settings.ENTRY_TOKEN}\n")

    write_file(target / "etc/hostname", f"{settings.BOOT_LABEL}\n")
    write_file(
        target / "etc/hosts",
        f"127.0.0.1\tlocalhost\n127.0.1.1\t{settings.BOOT_LABEL}\n",
    )

    # --- the package ------------------------------------------------------
    console.step("  hook(installed): the package")
    install_python_package(target, console)

    # Deliberately *not* the kernel-install plugins or the postinst hooks that
    # would call kernel-install. Nothing on this machine builds a UKI: the UKI
    # arrives built, inside the package the installer installs, and a
    # kernel-install run here would race it and overwrite it. See
    # ukis/postinst.py for what happens instead. The plugins remain in the
    # source tree as the libraries that postinst calls.
    console.info(
        f"  this machine builds no UKIs; it boots the one the {settings.PACKAGE_NAME} "
        "package installs"
    )

    # --- the installation itself ------------------------------------------
    # The target installs one package, and it is the only thing it ever
    # installs, so what has to be proven here is that installing it will work
    # with no apt lists and no network.
    console.step("  hook(installed): the installation")
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
