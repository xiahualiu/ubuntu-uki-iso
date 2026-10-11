"""Create the firmware entry for the installed standalone UKI."""

from __future__ import annotations

import re
from pathlib import Path

from .. import settings
from ..errors import BuildError
from ..log import Console
from ..proc import Runner
from .context import Context

_ENTRY_RE = re.compile(r"^Boot([0-9A-Fa-f]{4})\*?\s+" + re.escape(settings.BOOT_LABEL) + r"\s*$")


def run(ctx: Context) -> None:
    console, runner = ctx.console, ctx.runner
    console.step("5/5 boot entry")

    if not ctx.uki_rel:
        raise BuildError("no UKI path was recorded by the uki step")

    console.info(f"firmware entry: {settings.BOOT_LABEL!r} -> {ctx.uki_rel}")

    if ctx.dry_run:
        console.grey(f"would remove any existing entry labelled {settings.BOOT_LABEL!r}")
        console.grey(
            f"would run: efibootmgr --create --disk {ctx.root_disk} --part 1 "
            f"--label {settings.BOOT_LABEL} --loader '{ctx.uki_rel}'"
        )
        return

    _require_efi(runner)
    _remove_existing(runner, console)

    runner.run(
        "efibootmgr",
        "--create",
        "--disk",
        ctx.root_disk,
        "--part",
        "1",
        "--label",
        settings.BOOT_LABEL,
        "--loader",
        ctx.uki_rel,
    )

    # Read the entries back rather than assuming --create worked. On some
    # firmware efibootmgr reports success and the variable does not stick.
    entries = runner.capture("efibootmgr", "-v", check=False)
    if settings.BOOT_LABEL in entries:
        console.ok("boot entry created")
        for line in entries.splitlines():
            if settings.BOOT_LABEL in line:
                console.info(f"  {line}")
    else:
        console.warn(
            f"efibootmgr reported success but no {settings.BOOT_LABEL!r} entry is visible.\n"
            "The UKI is on the ESP and \\EFI\\BOOT\\BOOTX64.EFI points at it, so the\n"
            "machine still boots — but the entry may need creating from the firmware\n"
            "setup menu."
        )


def _require_efi(runner: Runner) -> None:
    if not Path("/sys/firmware/efi").is_dir():
        raise BuildError(
            "this system did not boot via UEFI, so there is no NVRAM to write a boot "
            "entry to.\n\nThe root filesystem and the UKI have been installed."
        )
    runner.require("efibootmgr")


def _remove_existing(runner: Runner, console: Console) -> None:
    """Keep the operation idempotent.

    Re-running the installer should not leave two entries pointing at two
    different UKIs with the firmware choosing arbitrarily.
    """
    entries = runner.capture("efibootmgr", check=False)
    for line in entries.splitlines():
        if match := _ENTRY_RE.match(line.strip()):
            number = match.group(1)
            console.grey(f"removing existing entry Boot{number}")
            runner.run("efibootmgr", "--bootnum", number, "--delete-bootnum", check=False)
            return
