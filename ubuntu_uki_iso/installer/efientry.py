"""
Verify the firmware entry created by the installed boot package.
"""

from __future__ import annotations

import subprocess
import sys

from .. import settings
from ..errors import BuildError
from .context import Context


def run(ctx: Context) -> None:
    print("==>", "5/5 verify boot entry")

    if not ctx.uki_rel:
        raise BuildError("no UKI path was recorded by the uki step")

    print(f"firmware entry: {settings.BOOT_LABEL!r} -> {ctx.uki_rel}")

    if ctx.dry_run:
        print("would verify the EFI entry created by the boot package postinst")
        return

    # Read the entries back rather than assuming --create worked. On some
    # firmware efibootmgr reports success and the variable does not stick.
    entries = subprocess.run(
        ["efibootmgr", "-v"], check=False, capture_output=True, text=True, timeout=120
    ).stdout.strip()
    if settings.BOOT_LABEL in entries:
        print("ok", "boot entry created")
        for line in entries.splitlines():
            if settings.BOOT_LABEL in line:
                print(f"  {line}")
    else:
        print(
            "WARN:",
            f"efibootmgr reported success but no {settings.BOOT_LABEL!r} entry is visible.\n"
            "The UKI is on the ESP and \\EFI\\BOOT\\BOOTX64.EFI points at it, so the\n"
            "machine still boots — but the entry may need creating from the firmware\n"
            "setup menu.",
            file=sys.stderr,
        )
