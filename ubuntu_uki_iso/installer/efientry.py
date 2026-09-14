"""Firmware boot entries, and the delayed blessing.

The design has a specific ordering here.

*install time* creates a new firmware entry for the UKI. The existing GRUB entry
is left alone.

*later*, only once the new UKI has actually booted, ``\\EFI\\BOOT\\BOOTX64.EFI``
can be repointed — which is what ``--bless`` does.

The entry points at the **stable** fallback path rather than at the versioned
UKI under ``\\EFI\\Linux\\``. That is deliberate: a versioned entry goes stale
the moment the kernel is upgraded, and eventually points at a UKI that
retention has pruned, leaving a boot entry that fails. The fallback path is
refreshed by the kernel-install plugin on every upgrade, so one entry stays
correct forever.
"""

from __future__ import annotations

import re
import shutil
from pathlib import Path

from .. import settings
from ..errors import BuildError, Refusal
from ..log import Console
from ..proc import Runner
from .context import Context

#: Where the running system records the kernel release that reached multi-user.
BOOT_MARKER = Path("/var/lib/xiahualab/booted")

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


# ---------------------------------------------------------------------------
# --bless
# ---------------------------------------------------------------------------


def bless(
    runner: Runner,
    console: Console,
    *,
    esp: Path | None = None,
    dry_run: bool | None = None,
) -> None:
    """Repoint the firmware fallback at the UKI of the running kernel.

    Mostly a repair tool now: the kernel-install plugin writes the fallback
    automatically at every kernel install, so this is for when something has
    gone wrong, or when someone has deliberately booted an older UKI and wants
    the fallback to match.

    It still refuses to act without evidence that the kernel in question has
    booted. Pointing the fallback at a kernel that has never come up is exactly
    the mistake the marker exists to prevent.
    """
    from .. import ukis

    console = console
    console.step("bless")
    if dry_run if dry_run is not None else runner.dry_run:
        console.info(f"{console.bold('DRY RUN')} — nothing will be written")

    if not _is_root():
        raise Refusal("--bless must run as root")
    runner.require("findmnt", "uname", "cp")

    esp = esp or _find_esp(runner, console)
    running = runner.capture("uname", "-r", check=False).strip()
    if not running:
        raise BuildError("could not determine the running kernel release")

    uki = _find_uki_for(esp, running)
    console.info(f"running kernel: {running}")
    console.info(f"UKI:            {uki}")

    marker = Path(BOOT_MARKER)
    if not marker.is_file():
        raise Refusal(
            f"{marker} does not exist.\n"
            "\n"
            "That file is written when this system boots successfully. Its absence\n"
            "means either that the installed system has never booted, or that this is\n"
            "not the installed system.\n"
            "\n"
            f"Refusing to replace {esp / 'EFI/BOOT/BOOTX64.EFI'} — that file is what the\n"
            "firmware falls back to when its boot entries are lost."
        )

    booted = marker.read_text().strip()
    console.info(f"last successful boot: {booted}")
    if booted != running:
        raise Refusal(
            f"the last successful boot was {booted!r}, but this kernel is {running!r}.\n"
            "Refusing to bless a UKI that has not been the one booting."
        )

    fallback = esp / ukis.FALLBACK_RELPATH
    backup = fallback.parent / ukis.BACKUP_RELPATH.name

    if runner.dry_run:
        console.grey(f"would back up {fallback} -> {backup} (once)")
        console.grey(f"would copy {uki} -> {fallback}")
        console.info(f"{console.green('Nothing was changed.')} Re-run with --apply.")
        return

    fallback.parent.mkdir(parents=True, exist_ok=True)

    # Keep the original exactly once. Overwriting the backup on a second run
    # would eventually destroy the only copy of a loader known to work.
    if fallback.is_file() and not backup.exists():
        shutil.copyfile(fallback, backup)
        console.ok(f"original preserved at {backup.name}")

    shutil.copyfile(uki, fallback)

    if fallback.stat().st_size != uki.stat().st_size:
        raise BuildError("the copy does not match the source")
    console.ok(f"{ukis.FALLBACK_RELPATH} now points at the booted UKI")
    console.info(f"To undo: cp {backup} {fallback}")


def _is_root() -> bool:
    import os

    return os.geteuid() == 0


def _find_esp(runner: Runner, console: Console) -> Path:
    target = runner.capture("findmnt", "-n", "-o", "TARGET", "/boot/efi", check=False)
    esp = Path(target.splitlines()[0].strip()) if target.strip() else Path("/boot/efi")
    if not (esp / "EFI").is_dir():
        raise BuildError(
            f"no EFI system partition at {esp}\n\n"
            "--bless runs on the installed system, where the ESP is normally mounted\n"
            "at /boot/efi. Mount it and try again."
        )
    return esp


def _find_uki_for(esp: Path, release: str) -> Path:
    candidates = sorted((esp / "EFI/Linux").glob(f"*{release}*.efi"))
    if not candidates:
        available = sorted(p.name for p in (esp / "EFI/Linux").glob("*.efi"))
        raise Refusal(
            f"no UKI for the running kernel ({release}) under {esp}/EFI/Linux\n"
            f"Available: {', '.join(available) if available else '(none)'}\n"
            "Refusing to bless a UKI that is not the one currently running."
        )
    return candidates[0]
