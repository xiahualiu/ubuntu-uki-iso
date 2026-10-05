"""Python definitions rendered for dracut, kernel-install and UKI command lines."""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass
from pathlib import Path

from .. import settings
from ..errors import ConfigError


def _write(path: Path, content: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


@dataclass(frozen=True)
class DracutConfig:
    hostonly: bool = False
    modules: tuple[str, ...] = ()
    drivers: tuple[str, ...] = ()
    omit_drivers: tuple[str, ...] = ("nouveau",)
    omit_modules: tuple[str, ...] = ()
    compression: str = "zstd"

    def __post_init__(self) -> None:
        tokens = (
            *self.modules,
            *self.drivers,
            *self.omit_drivers,
            *self.omit_modules,
            self.compression,
        )
        if any(not re.fullmatch(r"[A-Za-z0-9_-]+", token) for token in tokens):
            raise ConfigError("dracut names must be single identifiers")

    def render(self) -> str:
        lines = [f'hostonly="{"yes" if self.hostonly else "no"}"']
        for key, values in (
            ("add_dracutmodules", self.modules),
            ("drivers", self.drivers),
            ("omit_drivers", self.omit_drivers),
            ("omit_dracutmodules", self.omit_modules),
        ):
            if values:
                lines.append(f'{key}+=" {" ".join(values)} "')
        lines.append(f"compress={shlex.quote(self.compression)}")
        return "\n".join(lines) + "\n"

    def write(self, path: Path) -> Path:
        return _write(path, self.render())


@dataclass(frozen=True)
class KernelInstallConfig:
    layout: str = "uki"
    initrd_generator: str = "dracut"
    uki_generator: str = "ukify"

    def render(self) -> str:
        return (
            f"layout={self.layout}\ninitrd_generator={self.initrd_generator}\n"
            f"uki_generator={self.uki_generator}\n"
        )


@dataclass(frozen=True)
class KernelCommandLine:
    arguments: tuple[str, ...]

    def render(self) -> str:
        if any(
            not arg or any(char.isspace() for char in arg) or "\0" in arg for arg in self.arguments
        ):
            raise ConfigError("kernel command-line arguments must be nonempty single tokens")
        return " ".join(self.arguments)


def live_command_line() -> KernelCommandLine:
    return KernelCommandLine(
        (
            f"root=live:CDLABEL={settings.VOLID}",
            "rd.live.image",
            "rd.live.dir=/live",
            "rd.live.squashimg=filesystem.squashfs",
            "rd.live.overlay.overlayfs=1",
            # Keep data arrays untouched until the installer inspects them.
            "rd.md=0",
            "rd.luks=0",
            "rd.lvm=0",
            "console=tty0",
            "console=ttyS0,115200n8",
        )
    )


def installed_command_line() -> KernelCommandLine:
    return KernelCommandLine((f"root=PARTUUID={settings.ROOT_PARTUUID}", "ro", "quiet"))


def cmdline_live() -> str:
    return live_command_line().render()


def cmdline_installed() -> str:
    return installed_command_line().render()


def dracut_conf(variant: str) -> DracutConfig:
    if variant == "live":
        # The live root needs squashfs and a writable overlay; GPU stays in the rootfs.
        return DracutConfig(
            modules=("dmsquash-live", "overlayfs", "mdraid"), omit_modules=("ecryptfs",)
        )
    if variant == "installed":
        # Optional rebuilding on the server can inspect its real hardware.
        return DracutConfig(hostonly=True, modules=("mdraid",))
    if variant == "uki-build":
        # A build host cannot probe the server's storage topology.
        return DracutConfig(modules=("mdraid",), drivers=("nvme", "ahci", "raid0"))
    raise ConfigError(f"unknown dracut variant: {variant!r}")


def install_conf() -> KernelInstallConfig:
    return KernelInstallConfig()


def required_packages() -> tuple[str, ...]:
    """Apt packages required by the standalone dracut/ukify builder."""
    return ("dracut", "systemd-ukify", "systemd-boot-efi", "kmod", "zstd")
