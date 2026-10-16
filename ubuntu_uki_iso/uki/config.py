"""
Python definitions rendered for dracut and UKI command lines.
"""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass
from pathlib import Path

from .. import settings
from ..errors import ConfigError
from ..settings import Mode


@dataclass(frozen=True)
class DracutConfig:
    """
    Explicit dracut module and kernel driver lists for a non-host-only initramfs.

    Attributes:
        modules: Dracut modules to select, together with their required dependencies.
        drivers: Kernel drivers explicitly selected for the image.
        compression: Initramfs compression method.
    """

    modules: tuple[str, ...] = ()
    drivers: tuple[str, ...] = ()
    compression: str = "zstd"

    def __post_init__(self) -> None:
        """
        Validate module, driver and compression names as single identifiers.

        Raises:
            ConfigError: A name contains characters outside letters, digits, underscores
                and hyphens, or a module or driver list is empty.
        """
        tokens = (
            *self.modules,
            *self.drivers,
            self.compression,
        )
        if any(not re.fullmatch(r"[A-Za-z0-9_-]+", token) for token in tokens):
            raise ConfigError("dracut names must be single identifiers")
        if not self.modules or not self.drivers:
            raise ConfigError("dracut module and driver lists must not be empty")

    def render(self) -> str:
        """
        Render the settings in dracut configuration syntax.

        Returns:
            Configuration text with a trailing newline.
        """
        lines = ['hostonly="no"']
        for key, values in (
            ("dracutmodules", self.modules),
            ("drivers", self.drivers),
        ):
            lines.append(f'{key}=" {" ".join(values)} "')
        lines.append(f"compress={shlex.quote(self.compression)}")
        return "\n".join(lines) + "\n"

    def write(self, path: Path) -> Path:
        """
        Render and write the dracut configuration.

        Args:
            path: Destination configuration file.

        Returns:
            Path to the written file.
        """
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self.render(), encoding="utf-8")
        return path


@dataclass(frozen=True)
class KernelCommandLine:
    """
    Ordered kernel boot arguments rendered as a space-separated command line.

    Attributes:
        arguments: Individual boot arguments, each represented by one token.
    """

    arguments: tuple[str, ...]

    def render(self) -> str:
        """
        Validate and join the kernel boot arguments.

        Returns:
            Space-separated arguments without a trailing newline.

        Raises:
            ConfigError: An argument is empty or contains whitespace or a null character.
        """
        if any(
            not arg or any(char.isspace() for char in arg) or "\0" in arg for arg in self.arguments
        ):
            raise ConfigError("kernel command-line arguments must be nonempty single tokens")
        return " ".join(self.arguments)


def live_command_line() -> KernelCommandLine:
    """
    Define root discovery and kernel flags for booting the live medium.

    Returns:
        Arguments locating the live squashfs by volume label, enabling its writable
        overlay, disabling automatic storage activation and configuring consoles.
    """
    return KernelCommandLine(
        (
            f"root=live:CDLABEL={settings.VOLID}",
            "rd.live.image",
            "rd.live.dir=/payload",
            "rd.live.squashimg=base.squashfs",
            "rd.live.overlay.overlayfs=1",
            # Keep data arrays untouched until the installer inspects them.
            "rd.md=0",
            "rd.luks=0",
            "rd.lvm=0",
            "rd.shell",
            "rd.debug",
            "systemd.show_status=1",
            "console=ttyS0,115200n8",
            # Keep the physical monitor as /dev/console for local diagnostics.
            "console=tty0",
        )
    )


def installed_command_line() -> KernelCommandLine:
    """
    Define the root device and kernel flags for the installed system.

    Edit this function to customize installed-system boot parameters.

    Returns:
        Arguments selecting the configured GPT root partition label, initially mounting root
        read-only and enabling quiet boot.
    """
    return KernelCommandLine(
        (
            f"root=PARTLABEL={settings.ROOT_LABEL}",
            "ro",
            "quiet",
            "console=tty0",
            "console=ttyS0,115200n8",
        )
    )


def cmdline(mode: Mode) -> str:
    """
    Define the embedded kernel command line for the selected boot mode.

    Args:
        mode: Boot configuration variant, either live or installed.

    Returns:
        Rendered root device and kernel flags from the mode's command-line definition.

    Raises:
        ConfigError: The mode is not recognized or a boot argument is invalid.
    """
    if mode == Mode.LIVE:
        return live_command_line().render()
    if mode == Mode.INSTALLED:
        return installed_command_line().render()
    raise ConfigError(f"unknown UKI mode: {mode!r}")


def dracut_conf(variant: str) -> DracutConfig:
    """
    Select the initramfs configuration for a build or installed system.

    Args:
        variant: Configuration variant: live for the live medium, installed for
            a rebuild on the target, or uki-build for a prebuilt server UKI.

    Returns:
        Dracut settings with the modules and drivers required by the variant.

    Raises:
        ConfigError: The variant is not recognized.
    """
    modules = (
        "base",
        "systemd",
        "dracut-systemd",
        "udev-rules",
        "kernel-modules",
        "rootfs-block",
        "fs-lib",
        "shutdown",
        "mdraid",
    )
    drivers = ("nvme", "ahci", "raid0")
    if variant == Mode.LIVE:
        return DracutConfig(
            modules=(*modules, "dmsquash-live", "overlayfs"),
            # USB input is modular in our kernel and must work before switch-root.
            drivers=(*drivers, "usb_storage", "uas", "overlay", "usbhid", "hid_generic"),
        )
    if variant in (Mode.INSTALLED, "uki-build"):
        return DracutConfig(modules=modules, drivers=drivers)
    raise ConfigError(f"unknown dracut variant: {variant!r}")


def required_packages() -> tuple[str, ...]:
    """
    Return the apt packages required for the standalone UKI build.

    Returns:
        Package names checked by the UKI build preflight.
    """
    return (
        "apt",
        "dpkg",
        "dracut",
        "systemd-ukify",
        "systemd-boot-efi",
        "kmod",
        "mdadm",
        "dmsetup",
        "zstd",
    )
