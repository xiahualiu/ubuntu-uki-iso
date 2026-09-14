"""Keeping the ESP from filling up with kernels.

The ESP is 1 GB and a UKI is roughly 50 MB, so about ten to fifteen fit.
Without pruning, every kernel upgrade silently consumes another 50 MB until
``kernel-install`` fails — during an upgrade, on a machine that now has a
half-installed kernel. That is the worst possible moment to find out.

Two UKIs are never removed whatever the ordering says:

*the running kernel's*
    Removing the UKI of the kernel currently executing is the kind of thing
    that works fine until the next reboot.

*the one just installed*
    This is the subtle one, and it is why the plugin exists at all. It runs
    from kernel-install, so it sees the state immediately after a new UKI was
    written — which is exactly the moment when the running kernel is the *old*
    one and the new UKI is the one that must survive. Sorting by age alone
    would be correct here only by accident.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path

from .. import settings
from ..log import Console, get_console

#: Where kernel-install puts UKIs, relative to the boot root.
UKI_SUBDIR = Path("EFI/Linux")


@dataclass(frozen=True)
class Uki:
    path: Path
    mtime: float

    @property
    def name(self) -> str:
        return self.path.name


def find_ukis(boot_root: Path) -> list[Uki]:
    """Every UKI on the ESP, newest first."""
    directory = boot_root / UKI_SUBDIR
    if not directory.is_dir():
        return []

    found = []
    for path in directory.iterdir():
        if path.is_file() and path.suffix.lower() == ".efi":
            try:
                found.append(Uki(path, path.stat().st_mtime))
            except OSError:
                continue
    return sorted(found, key=lambda u: u.mtime, reverse=True)


def is_protected(uki: Uki, running_release: str, just_installed: str) -> bool:
    """Whether this UKI must survive regardless of age."""
    if running_release and running_release in uki.name:
        return True
    return bool(just_installed and just_installed in uki.name)


def prune(
    boot_root: Path,
    *,
    keep: int | None = None,
    running_release: str | None = None,
    just_installed: str = "",
    console: Console | None = None,
) -> list[Path]:
    """Remove the oldest UKIs beyond ``keep``, and return what was removed.

    ``keep`` counts UKIs *in addition to* the protected ones: a system that
    keeps three and has a protected running kernel ends up with four files.
    That is intentional — the alternative is that protecting a kernel silently
    reduces retention to two, which is the kind of arithmetic that eats the
    rollback you were counting on.
    """
    console = console or get_console()
    keep = settings.UKI_RETENTION if keep is None else keep
    running_release = running_release if running_release is not None else os.uname().release

    if keep < 1:
        console.warn(f"refusing to keep fewer than 1 UKI (asked for {keep})")
        return []

    ukis = find_ukis(boot_root)
    if len(ukis) <= keep:
        return []

    removed: list[Path] = []
    kept = 0
    for uki in ukis:
        if is_protected(uki, running_release, just_installed):
            continue
        if kept < keep:
            kept += 1
            continue
        try:
            uki.path.unlink()
        except OSError as exc:
            console.warn(f"could not remove {uki.path}: {exc}")
            continue
        removed.append(uki.path)

    return removed


def main(argv: list[str] | None = None) -> int:
    """kernel-install plugin entry point.

    kernel-install calls plugins as::

        <plugin> <verb> <kernel-version> <kernel-image> [<initrd>]

    Only ``add`` is interesting; ``remove`` already deletes what it should.
    """
    argv = sys.argv if argv is None else argv
    console = get_console()

    if len(argv) < 3:
        console.error("usage: 99-ubuntu-uki-iso-retention.install <verb> <kernel-version> ...")
        return 1

    verb, kernel_version = argv[1], argv[2]
    if verb != "add":
        return 0

    boot_root = Path(os.environ.get("KERNEL_INSTALL_BOOT_ROOT", "/boot/efi"))
    removed = prune(boot_root, just_installed=kernel_version, console=console)

    for path in removed:
        console.grey(f"  removed {path.name}")
    if removed:
        console.info(f"pruned {len(removed)} old UKI(s) from {boot_root / UKI_SUBDIR}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
