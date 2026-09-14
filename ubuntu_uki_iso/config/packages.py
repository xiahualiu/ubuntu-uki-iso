"""Package lists.

Two lists, for two different systems:

``live``
    The ISO's booted environment. This is the installer's operating system, so
    it is allowed to be comfortable — parted, an editor, a shell, mdadm. None
    of it reaches the target disk.

``installed``
    What gets unsquashed onto the target. Minimal server + Docker. Everything
    in it has to justify its place on the disk.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ..errors import ConfigError
from ..paths import data_file
from .kernel import list_entries

VARIANTS = ("live", "installed")


@dataclass(frozen=True)
class PackageList:
    """A parsed package list."""

    variant: str
    path: Path
    entries: tuple[str, ...]

    def __len__(self) -> int:
        return len(self.entries)

    def __iter__(self):
        return iter(self.entries)

    def comma_joined(self) -> str:
        """The form mmdebstrap's ``--include`` wants."""
        return ",".join(self.entries)

    def as_args(self) -> list[str]:
        """The form apt wants."""
        return list(self.entries)


def package_list(variant: str) -> PackageList:
    if variant not in VARIANTS:
        raise ConfigError(
            f"unknown package list {variant!r}; expected one of {', '.join(VARIANTS)}"
        )
    path = data_file("packages", f"{variant}.list")
    entries = tuple(list_entries(path))
    if not entries:
        raise ConfigError(f"{path} is empty")
    return PackageList(variant, path, entries)
