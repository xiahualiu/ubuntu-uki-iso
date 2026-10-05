"""Host requirements and rootfs package profiles."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass

from ..errors import ConfigError


@dataclass(frozen=True)
class PackageList:
    """An immutable package profile."""

    variant: str
    entries: tuple[str, ...]

    def __len__(self) -> int:
        return len(self.entries)

    def __iter__(self) -> Iterator[str]:
        return iter(self.entries)

    def comma_joined(self) -> str:
        """The form mmdebstrap's ``--include`` wants."""
        return ",".join(self.entries)

    def as_args(self) -> list[str]:
        """The form apt wants."""
        return list(self.entries)


def live_packages() -> PackageList:
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
            "udev",
            "dbus",
            "kmod",
            "locales",
            "tzdata",
            # --- kernel / initramfs tooling -------------------------------------------
            # dracut + ukify + the EFI stub are needed at BUILD time, because the ISO's own
            # UKI is produced inside this rootfs by kernel-install. The installed
            # profile receives a prebuilt UKI and does not include these tools.
            "dracut",
            "systemd-ukify",
            "systemd-boot-efi",
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
            # The kernel-install plugins and the installer itself are Python. On the
            # installed system this is on the kernel-upgrade path, so it is a real
            # dependency rather than a convenience.
            "python3-minimal",
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
    return PackageList(
        "installed",
        (
            # Packages for the INSTALLED system — minimal server + Docker.
            #
            # This list is unsquashed onto the target disk by `ubuntu-uki-iso install` from
            # the payload squashfs on the ISO. It is deliberately lean: no GNOME, no snapd,
            # no display server. Everything here has to justify its place on the disk.
            #
            # Neither the kernel nor the UKI is listed, and neither is anything that could
            # build one. Both arrive in the UKI package, which is the one thing this list's
            # system ever installs — as its first install and as every later kernel update.
            # That is also why there is no dracut, no ukify and no EFI stub here: nothing
            # on this machine compiles, assembles or probes anything.
            # --- init and core ---------------------------------------------------------
            "systemd",
            "systemd-sysv",
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
            # efibootmgr, so a firmware entry can be recreated from the machine itself if
            # the NVRAM ever loses it. Deliberately absent: dracut, systemd-ukify and
            # systemd-boot-efi. A kernel upgrade here installs a newer version of the UKI
            # package, which brings the UKI already built — nothing on this machine
            # regenerates one, so nothing here needs the tools to.
            "efibootmgr",
            # --- GPU -------------------------------------------------------------------
            # DRM_NOUVEAU is built as a module and kept in the rootfs. Without firmware it
            # loads but cannot accelerate, which would make keeping it pointless.
            "linux-firmware",
            # --- interpreter -----------------------------------------------------------
            # The UKI package's postinst is Python, and so are `--bless` and the
            # boot marker. The postinst is on the install path — it is what puts the UKI
            # on the ESP — so this is a real dependency rather than a convenience, and the
            # package declares it in Depends.
            "python3-minimal",
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


def host_package_groups() -> dict[str, tuple[str, ...]]:
    return {
        "build": (
            # Everything needed to produce the artifacts: the kernel, the two rootfs
            # images, the UKIs and the ISO.
            # --- kernel build ----------------------------------------------------------
            # The standard dep set, plus dwarves for pahole (CONFIG_DEBUG_INFO_BTF stays
            # on) and debhelper/fakeroot/dpkg-dev/devscripts/equivs for `make bindeb-pkg`,
            # which shells out to scripts/package/mkdebian.
            "build-essential",
            "bc",
            "bison",
            "flex",
            "libssl-dev",
            "libelf-dev",
            "libncurses-dev",
            "dwarves",
            "debhelper",
            "fakeroot",
            "dpkg-dev",
            "devscripts",
            "equivs",
            # --- fetching the source ---------------------------------------------------
            "git",
            "wget",
            "curl",
            "ca-certificates",
            "gnupg",
            "ubuntu-keyring",
            # --- image build -----------------------------------------------------------
            # mmdebstrap bootstraps both rootfs images; xorriso makes the hybrid ISO;
            # mtools and dosfstools build the FAT EFI image without a loop device or a
            # mount. sbsigntool is kept because a future Secure Boot story would need it.
            "mmdebstrap",
            "xorriso",
            "squashfs-tools",
            "mtools",
            "dosfstools",
            "sbsigntool",
            # --- the UKIs --------------------------------------------------------------
            # dracut builds the initramfs, ukify assembles the UKI, and systemd-boot-efi
            # provides /usr/lib/systemd/boot/efi/linuxx64.efi.stub — the PE stub the UKI is
            # built around, without installing a bootloader.
            "dracut",
            "systemd-ukify",
            "systemd-boot-efi",
            # --- packing and unpacking ------------------------------------------------
            # dpkg-deb extracts the kernel package's files and builds the UKI package. It
            # is used to read archives, never to install them.
            "dpkg-dev",
            "zstd",
            "xz-utils",
            "bzip2",
            "cpio",
            "kmod",
            "rsync",
            # --- runtime ---------------------------------------------------------------
            # Every step in this project is a Python module, so the interpreter is the
            # runtime rather than a transitive detail.
            "python3",
        ),
        "test": (
            # Only needed to run the QEMU suite. Kept separate because a machine building a
            # kernel should not be told it is missing QEMU.
            "qemu-system-x86",
            "ovmf",
            "expect",
        ),
    }


def host_packages(*groups: str) -> tuple[str, ...]:
    profiles = host_package_groups()
    wanted = groups or ("build",)
    unknown = [group for group in wanted if group not in profiles]
    if unknown:
        raise ConfigError(f"unknown host package group(s): {', '.join(unknown)}")
    return tuple(dict.fromkeys(package for group in wanted for package in profiles[group]))


def package_list(variant: str) -> PackageList:
    profiles = {"live": live_packages, "installed": installed_packages}
    if variant not in profiles:
        raise ConfigError(f"unknown package profile: {variant!r}")
    return profiles[variant]()
