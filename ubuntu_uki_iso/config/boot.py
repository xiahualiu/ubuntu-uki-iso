"""Boot configuration: the command line, and dracut.

The command line is the part of this project that cannot be edited at boot.
There is no bootloader and no menu, so changing a boot parameter means
rebuilding the UKI. That is the trade for having nothing between the firmware
and the kernel, and it is why these are versioned templates rather than
something edited in place on the machine.

Both command lines are **static** — everything they name is known when the ISO
is built, not when the target's disk is formatted. That is what lets the
target's UKI be built here rather than there.

* the live one names the ISO's volume label, substituted from settings so the
  label burned into the ISO and the label dracut searches for cannot drift
* the installed one names a PARTUUID, which the installer writes into the
  partition table from the same constant — so a partition that does not exist
  yet still has an identity known at build time

Rendering is still the only supported way to get a command line out, because
each of those substitutions is wrong in a way that produces a machine that does
not boot: a label that does not match sends dracut hunting for a medium that is
not there, and a PARTUUID that does not match panics with "unable to find root
device" — after a UKI that looked perfectly fine was built.
"""

from __future__ import annotations

from pathlib import Path

from .. import settings
from ..errors import ConfigError
from ..paths import data_file

#: Substituted with settings.VOLID.
VOLID_PLACEHOLDER = "@@VOLID@@"

#: Substituted with settings.ROOT_PARTUUID.
ROOT_PARTUUID_PLACEHOLDER = "@@ROOT_PARTUUID@@"

_PLACEHOLDERS = (VOLID_PLACEHOLDER, ROOT_PARTUUID_PLACEHOLDER)

#: Which dracut configuration belongs to which build.
_DRACUT_CONFS = {
    "live": "00-live",
    "installed": "10-installed",
    "uki-build": "20-uki-build",
}


def _render(template: str, substitutions: dict[str, str], source: str) -> str:
    rendered = template
    for placeholder, value in substitutions.items():
        rendered = rendered.replace(placeholder, value)

    leftover = [p for p in _PLACEHOLDERS if p in rendered]
    if leftover:
        raise ConfigError(
            f"{source} still contains unsubstituted placeholder(s): {', '.join(leftover)}\n"
            "A UKI built from this command line would not find what it is looking for."
        )
    return rendered


def cmdline_live() -> str:
    """The live image's command line.

    The volume label comes from settings rather than being written into the
    file, so the label burned into the ISO and the label dracut searches for
    cannot drift apart.
    """
    path = data_file("cmdline", "live")
    return _render(
        path.read_text(encoding="utf-8").strip(),
        {VOLID_PLACEHOLDER: settings.VOLID},
        str(path),
    )


def cmdline_installed() -> str:
    """The installed image's command line — finished, not a template.

    There is nothing left to substitute at install time: the root partition's
    GUID is a constant this project writes into the table itself, so the
    command line is complete the moment the ISO is built. The UKI built from it
    can therefore be built here, which is the whole point.
    """
    path = data_file("cmdline", "installed")
    return _render(
        path.read_text(encoding="utf-8").strip(),
        {ROOT_PARTUUID_PLACEHOLDER: settings.ROOT_PARTUUID},
        str(path),
    )


def dracut_conf(variant: str) -> Path:
    """The dracut configuration for a variant.

    They differ in a way that matters, and it is all about which machine is
    building:

    * ``live`` — built here, has to boot on whatever machine the ISO is
      inserted into, so it is not host-only
    * ``installed`` — shipped to the target, and host-only, because if it is
      ever used there it is being used on the machine it describes
    * ``uki-build`` — used *here* to build the target's UKI, so it must not be
      host-only either: this machine is not that machine, and a probe of it
      would find none of the target's hardware
    """
    if variant not in _DRACUT_CONFS:
        raise ConfigError(
            f"unknown dracut variant {variant!r}; expected one of {', '.join(_DRACUT_CONFS)}"
        )
    return data_file("dracut", f"{_DRACUT_CONFS[variant]}.conf")
