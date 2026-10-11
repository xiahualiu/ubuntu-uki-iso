"""Package profiles, services and compression settings for rootfs images."""

from ..config.packages import PackageList
from ..errors import ConfigError


def live_packages() -> PackageList:
    """
    Return packages for the live installer and diagnostic environment.
    """
    return PackageList(
        "live",
        (
            # Packages for the LIVE environment — the one the ISO boots into.
            #
            # This is the installer's operating system, not the installed system. It is
            # allowed to be comfortable: parted, an editor, a shell, mdadm. None of it
            # reaches the target disk; the target gets installed_packages() profile unsquashed
            # onto it instead.
            #
            # Deliberately absent: snapd, any display server, linux-firmware (the live
            # environment talks to the serial console, not the GPU).
            # --- init and core ---------------------------------------------------------
            "systemd",
            "systemd-sysv",
            "systemd-resolved",
            "udev",
            "dbus",
            "kmod",
            "locales",
            "tzdata",
            # --- the installer's toolbox ----------------------------------------------
            "mdadm",
            "xfsprogs",
            "e2fsprogs",
            "dosfstools",
            "util-linux",
            "parted",
            "gdisk",
            "squashfs-tools",
            "efibootmgr",
            "rsync",
            "tar",
            "cpio",
            "zstd",
            "xz-utils",
            "gzip",
            # --- inspection ------------------------------------------------------------
            # The array gets looked at before anything is written to it, and half of that
            # is asking the disks how they feel about it.
            "smartmontools",
            "pciutils",
            "usbutils",
            "lsscsi",
            # --- interpreter -----------------------------------------------------------
            # The live installer uses the standard-library Python package.
            "python3",
            # --- shell quality of life -------------------------------------------------
            "bash",
            "bash-completion",
            "less",
            "nano",
            "vim-tiny",
            "procps",
            "psmisc",
            "file",
            "tree",
            "jq",
            # --- network ---------------------------------------------------------------
            "iproute2",
            "iputils-ping",
            "curl",
            "ca-certificates",
            "openssh-client",
            # --- console ---------------------------------------------------------------
            "kbd",
            "console-setup",
        ),
    )


def installed_packages() -> PackageList:
    """
    Return packages for the persistent server and container runtime.
    """
    return PackageList(
        "installed",
        (
            # Packages for the INSTALLED system — minimal server + Docker.
            #
            # This list is unsquashed onto the target disk by `ubuntu-uki-iso install` from
            # the payload squashfs on the ISO. It is deliberately lean: no GNOME, no snapd,
            # no display server. Everything here has to justify its place on the disk.
            #
            # The builder copies matching kernel modules from the input image deb.
            # UKIs are built separately; neither profile needs boot image build tools.
            # --- init and core ---------------------------------------------------------
            "systemd",
            "systemd-sysv",
            "systemd-resolved",
            "udev",
            "dbus",
            "kmod",
            "locales",
            "tzdata",
            # --- containers ------------------------------------------------------------
            # docker.io from the Ubuntu archive is 29.1.3 on resolute, which is within a
            # point release of what this box runs today. Taking it from the archive keeps
            # the build reproducible from a single mirror with no third-party keyring —
            # which matters more here than the version, because the whole premise of this
            # project is that the image can be rebuilt from source and come out the same.
            "docker.io",
            # --- storage ---------------------------------------------------------------
            "mdadm",
            "e2fsprogs",
            "dosfstools",
            "util-linux",
            "smartmontools",
            # --- boot ------------------------------------------------------------------
            # Firmware boot entries can be repaired from the installed system.
            "efibootmgr",
            # --- GPU -------------------------------------------------------------------
            # Firmware for devices and separately installed graphics drivers.
            "linux-firmware",
            # --- interpreter -----------------------------------------------------------
            # Runtime administration helpers use the standard-library Python package.
            "python3",
            # --- administration --------------------------------------------------------
            "openssh-server",
            "sudo",
            "bash-completion",
            "less",
            "nano",
            "vim-tiny",
            "procps",
            "psmisc",
            "curl",
            "ca-certificates",
            "iproute2",
            "iputils-ping",
            "pciutils",
            "usbutils",
        ),
    )


def package_list(mode: str) -> PackageList:
    """
    Select the package profile for a rootfs image.

    Args:
        mode: Image variant, either live or installed.

    Returns:
        Packages to install with mmdebstrap.
    """
    if mode == "live":
        return live_packages()
    if mode == "installed":
        return installed_packages()
    raise ConfigError(f"unknown rootfs mode: {mode!r}")


def required_packages() -> tuple[str, ...]:
    """
    Return system packages required for bootstrapping and compressing rootfs images.
    """
    return ("apt", "dpkg", "mmdebstrap", "squashfs-tools", "kmod", "systemd", "ubuntu-keyring")


def services(mode: str) -> tuple[str, ...]:
    """
    Return services to enable in the rootfs without starting them on the build host.
    """
    common = ("systemd-networkd.service", "systemd-resolved.service")
    if mode == "live":
        return (*common, "serial-getty@ttyS0.service")
    if mode == "installed":
        return (
            *common,
            "docker.service",
            "ssh-host-keys.service",
            "ssh.service",
            "mdmonitor.service",
        )
    raise ConfigError(f"unknown rootfs mode: {mode!r}")


def compression_level() -> int:
    """
    Return the Zstd level used for development rootfs compression.
    """
    return 9


def squashfs_excludes() -> tuple[str, ...]:
    """
    Return transient contents excluded from SquashFS while retaining mount directories.
    """
    return (
        "proc/*",
        "sys/*",
        "dev/*",
        "run/*",
        "tmp/*",
        "var/tmp/*",
        "var/cache/apt/*",
        "var/lib/apt/lists/*",
        "boot/efi/*",
    )
