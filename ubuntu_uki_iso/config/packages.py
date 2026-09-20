"""Package lists.

Three lists, for three different machines:

``live``
    The ISO's booted environment. This is the installer's operating system, so
    it is allowed to be comfortable — parted, an editor, a shell, mdadm. None
    of it reaches the target disk.

``installed``
    What gets unsquashed onto the target. Minimal server + Docker. Everything
    in it has to justify its place on the disk.

``host``
    What the machine *running this tool* needs — a CI runner, or a container on
    one. Different in kind from the other two: it is a declaration the tool
    checks against, never something it installs. See :func:`host_packages`.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from ..errors import ConfigError
from ..paths import data_file
from .kernel import list_entries

VARIANTS = ("live", "installed")

#: The host requirements, grouped by what needs them.
HOST_LIST = "packages/host.list"

#: A group header in that file: ``# --- group: build ---``.
_GROUP_RE = re.compile(r"^\s*#\s*-+\s*group:\s*([a-z][a-z0-9-]*)", re.IGNORECASE)


@dataclass(frozen=True)
class PackageList:
    """A parsed package list."""

    variant: str
    path: Path
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


def grouped_entries(path: Path) -> dict[str, list[str]]:
    """Entries grouped by ``# --- group: name ---`` headers.

    The host requirements have a shape the rootfs lists do not: a machine
    building a kernel does not need QEMU, and saying so should not mean a second
    file. Entries appearing before any header belong to the group named "".
    """
    groups: dict[str, list[str]] = {}
    current = ""
    for raw in path.read_text(encoding="utf-8").splitlines():
        if match := _GROUP_RE.match(raw):
            current = match.group(1).lower()
            groups.setdefault(current, [])
            continue
        line = raw.split("#", 1)[0].strip()
        if line:
            groups.setdefault(current, []).append(line)
    return groups


def host_packages(*groups: str) -> tuple[str, ...]:
    """The apt packages this tool needs on the machine it runs on.

    Deduplicated but order-preserving: a package can belong to more than one
    group, and the file is meant to be read by a person. Checked with
    :meth:`ubuntu_uki_iso.proc.Runner.require_packages`, never installed.
    """
    wanted = groups or ("build",)
    parsed = grouped_entries(data_file(*HOST_LIST.split("/")))

    unknown = [name for name in wanted if name not in parsed]
    if unknown:
        raise ConfigError(
            f"unknown host package group(s): {', '.join(unknown)}\n"
            f"{HOST_LIST} declares: {', '.join(sorted(parsed))}"
        )

    entries: list[str] = []
    for name in wanted:
        for entry in parsed[name]:
            if entry not in entries:
                entries.append(entry)
    return tuple(entries)


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
