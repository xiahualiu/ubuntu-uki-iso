"""
Package profiles, services and compression settings for rootfs images.
"""

from ..errors import ConfigError
from ..settings import Mode
from .packages import PackageList

# -- distributions ----------------------------------------------------------

UBUNTU_SUITE = "resolute"
UBUNTU_MIRROR = "http://archive.ubuntu.com/ubuntu"
UBUNTU_COMPONENTS = "main,universe"


# Both images use the same hardware, administration and server packages.
BASE_PACKAGES = (
    # Init, networking and core runtime.
    "systemd",
    "systemd-sysv",
    "systemd-resolved",
    "netplan.io",
    "network-manager",
    "udev",
    "dbus",
    "kmod",
    "locales",
    "tzdata",
    "python3",
    # Server services; live mode disables their automatic startup.
    "docker.io",
    "openssh-server",
    "openssh-client",
    "sudo",
    # Declared server hardware: NVIDIA GPU; igc, NVMe and AHCI need no blobs.
    "linux-firmware-nvidia-graphics",
    # Storage and recovery tools.
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
    "smartmontools",
    "pciutils",
    "usbutils",
    "lsscsi",
    # Console and administration.
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
    "iproute2",
    "iputils-ping",
    "curl",
    "ca-certificates",
    "kbd",
    "console-setup",
)


def package_list(mode: Mode) -> PackageList:
    """
    Use the shared base, adding venv support only for the live installer.
    """
    if mode not in (Mode.LIVE, Mode.INSTALLED):
        raise ConfigError(f"unknown rootfs mode: {mode!r}")
    extra = ("python3-venv",) if mode == Mode.LIVE else ()
    return PackageList(mode, BASE_PACKAGES + extra)


def required_packages() -> tuple[str, ...]:
    """
    Return system packages required for bootstrapping and compressing rootfs images.
    """
    return (
        "apt",
        "dpkg",
        "mmdebstrap",
        "squashfs-tools",
        "kmod",
        "systemd",
        "ubuntu-keyring",
        "python3-pip",
        "python3-setuptools",
        "python3-wheel",
    )


def services(mode: Mode) -> tuple[str, ...]:
    """
    Return services to enable in the rootfs without starting them on the build host.
    """
    common = ("NetworkManager.service", "systemd-resolved.service")
    if mode == Mode.LIVE:
        return (*common, "serial-getty@ttyS0.service")
    if mode == Mode.INSTALLED:
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
