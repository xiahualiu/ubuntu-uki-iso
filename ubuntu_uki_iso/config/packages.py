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
            # mount.
            "mmdebstrap",
            "xorriso",
            "squashfs-tools",
            "mtools",
            "dosfstools",
            # --- the UKIs --------------------------------------------------------------
            # dracut builds the initramfs, ukify assembles the UKI, and systemd-boot-efi
            # provides /usr/lib/systemd/boot/efi/linuxx64.efi.stub — the PE stub the UKI is
            # built around, without installing a bootloader.
            "dracut",
            "systemd-ukify",
            "systemd-boot-efi",
            # --- packing and unpacking ------------------------------------------------
            # dpkg-deb extracts the kernel package's files. It
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
    from ..rootfs.config import installed_packages, live_packages

    profiles = {"live": live_packages, "installed": installed_packages}
    if variant not in profiles:
        raise ConfigError(f"unknown package profile: {variant!r}")
    return profiles[variant]()
