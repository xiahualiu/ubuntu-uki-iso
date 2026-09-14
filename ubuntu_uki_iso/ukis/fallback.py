"""Putting the current UKI where firmware looks by default.

``kernel-install`` with ``layout=uki`` writes the UKI to
``\\EFI\\Linux\\<machine-id>-<version>.efi``. Firmware does not look there
unless something tells it to — normally an NVRAM boot entry, which is what
``efibootmgr`` creates. That works, but it makes the machine's ability to boot
depend on NVRAM state, which is lost by a CMOS reset, a firmware update, or
moving the disk to another machine.

``\\EFI\\BOOT\\BOOTX64.EFI`` is the path UEFI firmware falls back to with no
configuration at all. Keeping the current UKI there means the machine boots
without anyone opening the firmware setup menu, and keeps booting after the
NVRAM entries are gone.

This is not only convenience. The installer formats the ESP, which destroys
whatever loader was there — on this machine, shim and GRUB. After that install
the only ways in are the new NVRAM entry and this file. If ``efibootmgr``
fails silently, or the board ignores the entry, this file is the difference
between a booting machine and a dead one.
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

from ..log import Console, get_console

#: The firmware's default path, relative to the boot root.
FALLBACK_RELPATH = Path("EFI/BOOT/BOOTX64.EFI")

#: Where a foreign loader is preserved, once.
BACKUP_RELPATH = Path("EFI/BOOT/BOOTX64.EFI.pre-ubuntu-uki-iso")

#: Enough of the file to cover the PE headers and section table, where the
#: section names live. The whole file is ~50 MB and none of the rest of it is
#: needed to answer this question.
_HEADER_SCAN_BYTES = 64 * 1024

#: systemd UKIs carry a ".cmdline" section. Bootloaders do not.
_UKI_MARKER = b".cmdline"


def find_uki_for(boot_root: Path, kernel_version: str) -> Path | None:
    """The UKI kernel-install just wrote for ``kernel_version``.

    Matched on the version rather than reconstructed from
    ``KERNEL_INSTALL_MACHINE_ID``, because kernel-install only sets that in
    some code paths — the filename is ``<machine-id>-<version>.efi`` when it is
    set and ``<version>.efi`` when it is not.
    """
    directory = boot_root / "EFI/Linux"
    if not directory.is_dir():
        return None
    for path in sorted(directory.iterdir()):
        if path.is_file() and path.suffix.lower() == ".efi" and kernel_version in path.name:
            return path
    return None


def looks_like_uki(path: Path) -> bool:
    """Whether a file is a UKI rather than somebody's bootloader.

    Used to decide whether the existing ``BOOTX64.EFI`` is worth preserving.
    Our own file from a previous upgrade is not; shim or GRUB is.
    """
    try:
        with open(path, "rb") as handle:
            head = handle.read(_HEADER_SCAN_BYTES)
    except OSError:
        return False
    return _UKI_MARKER in head


def install(
    boot_root: Path,
    kernel_version: str,
    *,
    console: Console | None = None,
) -> Path | None:
    """Point the firmware fallback at the UKI for ``kernel_version``.

    Returns the destination, or ``None`` if there was no UKI to install.
    """
    console = console or get_console()

    source = find_uki_for(boot_root, kernel_version)
    if source is None:
        console.warn(
            f"no UKI for {kernel_version} under {boot_root / 'EFI/Linux'}; "
            f"leaving {FALLBACK_RELPATH} alone"
        )
        return None

    destination = boot_root / FALLBACK_RELPATH
    destination.parent.mkdir(parents=True, exist_ok=True)

    _preserve_foreign_loader(destination, console)

    shutil.copyfile(source, destination)

    # Verify rather than trusting the copy. A truncated fallback is worse than
    # no fallback, because it looks like one.
    source_size = source.stat().st_size
    dest_size = destination.stat().st_size
    if source_size != dest_size:
        raise OSError(
            f"fallback copy is the wrong size ({source_size} vs {dest_size} bytes): {destination}"
        )

    return destination


def _preserve_foreign_loader(destination: Path, console: Console) -> None:
    """Back up an existing non-UKI loader, exactly once.

    The installer formats the ESP, so on a normal install there is nothing here
    and this does nothing. It matters for the other path: adding the UKI to an
    existing system, where ``\\EFI\\BOOT\\BOOTX64.EFI`` holds shim and
    overwriting it without a copy would remove the fallback that makes the
    machine recoverable.

    The backup is never overwritten. A second run that clobbered it would
    eventually destroy the only copy of the loader known to work.
    """
    if not destination.is_file():
        return

    backup = destination.parent / BACKUP_RELPATH.name
    if backup.exists():
        return
    if looks_like_uki(destination):
        return

    shutil.copyfile(destination, backup)
    console.info(f"preserved the existing loader at {backup.name}")


def main(argv: list[str] | None = None) -> int:
    """kernel-install plugin entry point."""
    argv = sys.argv if argv is None else argv
    console = get_console()

    if len(argv) < 3:
        console.error("usage: 95-ubuntu-uki-iso-fallback.install <verb> <kernel-version> ...")
        return 1

    verb, kernel_version = argv[1], argv[2]
    if verb != "add":
        return 0

    boot_root = Path(os.environ.get("KERNEL_INSTALL_BOOT_ROOT", "/boot/efi"))

    try:
        installed = install(boot_root, kernel_version, console=console)
    except OSError as exc:
        console.error(str(exc))
        return 1

    if installed is not None:
        console.info(f"{installed.relative_to(boot_root)} <- UKI for {kernel_version}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
