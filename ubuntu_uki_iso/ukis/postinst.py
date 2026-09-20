"""Putting the packaged UKI onto the ESP — the installed system's only boot step.

This is what dpkg runs when the UKI package is configured: on the first
install, and again on every upgrade. That is deliberate, and it is the whole
reason the UKI travels as a package. The machine's first install and its future
kernel updates are the same operation, so there is no path that runs once and
is never exercised again.

Nothing here builds anything. The package carries the UKI and the kernel's
modules, so what is left is the three things that have to happen on the machine
that is going to boot:

* put the UKI under ``EFI/Linux``, where the firmware entry, ``--bless`` and
  retention all look for it
* refresh ``\\EFI\\BOOT\\BOOTX64.EFI`` — the path firmware uses with no
  configuration at all, and the only one that survives a lost NVRAM
  (:mod:`ubuntu_uki_iso.ukis.fallback`)
* prune older UKIs, because the ESP is 1 GB and holds about a dozen
  (:mod:`ubuntu_uki_iso.ukis.retention`)

The modules arrive as ordinary package payload under ``/lib/modules/<release>``
and need nothing beyond ``depmod``.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

from .. import settings
from ..errors import ConfigError, XiahualabError
from ..log import Console, get_console
from . import fallback, retention

#: Where the package keeps the UKI and the release it belongs to.
PACKAGE_DIR = Path("/usr/lib/ubuntu-uki-iso")
PACKAGED_UKI = PACKAGE_DIR / "uki.efi"
RELEASE_FILE = PACKAGE_DIR / "release"

#: Where the kernel's modules land. Absolute, because this runs as a package
#: maintainer script and is talking about the system it is installed on — named
#: here so a test can point it at something that exists.
MODULES_DIR = Path("/lib/modules")


def release() -> str:
    """The kernel release this package carries.

    Read from a file written when the package was built, rather than inferred
    from ``/lib/modules``: after an upgrade that has not pruned yet, more than
    one release is present there, and picking one of them by guessing is how
    the wrong UKI ends up on the ESP.
    """
    try:
        text = RELEASE_FILE.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise ConfigError(f"the package has no release file at {RELEASE_FILE}: {exc}") from exc

    if not text:
        raise ConfigError(f"{RELEASE_FILE} is empty; the package is malformed")
    return text


def install(boot_root: Path, *, console: Console | None = None) -> Path:
    """Place the packaged UKI, refresh the fallback, and prune.

    Refuses if the ESP is not actually mounted where it is expected. That check
    earns its place: writing the UKI into an ordinary directory on the root
    filesystem would report success and leave a machine that boots whatever was
    there before — or nothing.
    """
    console = console or get_console()
    version = release()

    modules = MODULES_DIR / version
    if not modules.is_dir():
        raise ConfigError(
            f"the package names kernel {version}, but {modules} is not there.\n"
            "Its payload and its metadata disagree, so the package is malformed — and\n"
            "installing its UKI would give the machine a kernel with no modules to load."
        )

    if not PACKAGED_UKI.is_file():
        raise ConfigError(f"the package carries no UKI at {PACKAGED_UKI}")

    if not os.path.ismount(boot_root):
        raise ConfigError(
            f"the EFI system partition is not mounted at {boot_root}.\n"
            "\n"
            "Refusing to write a UKI into a directory that is not the ESP. Mount it and\n"
            "install again: a UKI written onto the root filesystem is one the firmware\n"
            "will never look at, and nothing about the install would say so."
        )

    destination = boot_root / retention.UKI_SUBDIR / f"{settings.ENTRY_TOKEN}-{version}.efi"
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(PACKAGED_UKI, destination)

    # Verify rather than trusting the copy: a truncated UKI is worse than none,
    # because it looks like one.
    expected = PACKAGED_UKI.stat().st_size
    actual = destination.stat().st_size
    if actual != expected:
        raise ConfigError(f"{destination} is {actual} bytes; the UKI is {expected} — truncated")

    console.ok(f"{destination.relative_to(boot_root)} ({expected // 1024 // 1024} MB)")

    fallback.install(boot_root, version, console=console)
    _depmod(version, console)

    for path in retention.prune(boot_root, just_installed=version, console=console):
        console.grey(f"pruned {path.name}")

    return destination


def _depmod(version: str, console: Console) -> None:
    """Rebuild the module dependency map. Best effort, on purpose.

    The package ships the ``modules.dep`` its own build generated, and the paths
    in it are relative to the modules directory, so a machine with no ``depmod``
    still loads modules by name. This runs anyway because a machine that can
    keep the map current should, and because a failure here costs a warning
    rather than a boot.
    """
    if shutil.which("depmod") is None:
        console.grey("depmod is not installed; using the modules.dep the package ships")
        return

    result = subprocess.run(
        ["depmod", version], check=False, capture_output=True, text=True, timeout=300
    )
    if result.returncode != 0:
        console.warn(f"depmod {version} failed: {result.stderr.strip() or result.returncode}")


def main(argv: list[str] | None = None) -> int:
    """dpkg postinst entry point.

    dpkg calls this as ``postinst configure <most-recently-configured-version>``.
    Every other verb — ``abort-upgrade``, ``abort-remove``, ``disappear`` —
    means the package is *not* being installed, so the ESP is left alone.
    """
    argv = sys.argv if argv is None else argv
    console = get_console()

    if len(argv) < 2 or argv[1] != "configure":
        return 0

    try:
        install(Path(settings.ESP_MOUNT), console=console)
    except XiahualabError as exc:
        console.error(str(exc))
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
