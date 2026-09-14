"""Boot configuration: the command line, and dracut.

The command line is the part of this project that cannot be edited at boot.
There is no bootloader and no menu, so changing a boot parameter means
rebuilding the UKI. That is the trade for having nothing between the firmware
and the kernel, and it is why these are versioned templates rather than
something edited in place on the machine.

Both cmdline files are templates. Rendering them is the only supported way to
get a command line out, because the two substitutions they need — the volume
label and the target's root UUID — are exactly the two things that are wrong
in a way that produces a machine that does not boot:

* a volume label that does not match the ISO's sends dracut hunting for a
  medium that is not there
* a ``root=UUID=`` that does not match the filesystem panics with "unable to
  find root device", after a UKI that looked perfectly fine was built
"""

from __future__ import annotations

from pathlib import Path

from .. import settings
from ..errors import ConfigError
from ..paths import data_file

#: Substituted with the target filesystem's UUID at install time.
ROOT_UUID_PLACEHOLDER = "@@ROOT_UUID@@"

#: Substituted with settings.VOLID.
VOLID_PLACEHOLDER = "@@VOLID@@"


def _render(template: str, substitutions: dict[str, str], source: str) -> str:
    rendered = template
    for placeholder, value in substitutions.items():
        rendered = rendered.replace(placeholder, value)

    leftover = [p for p in (ROOT_UUID_PLACEHOLDER, VOLID_PLACEHOLDER) if p in rendered]
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


def cmdline_installed_unrendered() -> str:
    """The installed command line, still carrying ``@@ROOT_UUID@@``.

    Used for the dry-run plan, where the real UUID does not exist yet.
    """
    return data_file("cmdline", "installed").read_text(encoding="utf-8").strip()


def render_installed_cmdline(root_uuid: str) -> str:
    """The installed command line with the target's real root UUID.

    Refuses an empty or placeholder-looking UUID rather than producing a UKI
    that builds cleanly and panics at boot.
    """
    if not root_uuid or root_uuid == ROOT_UUID_PLACEHOLDER:
        raise ConfigError(f"refusing to render an installed cmdline with root UUID {root_uuid!r}")
    return _render(
        cmdline_installed_unrendered(),
        {ROOT_UUID_PLACEHOLDER: root_uuid},
        "cmdline/installed",
    )


def dracut_conf(variant: str) -> Path:
    """The dracut configuration for ``live`` or ``installed``.

    They differ in a way that matters: the live initramfs is built in a
    container and has to boot on whatever machine the ISO is inserted into, so
    it is not host-only; the installed one is generated on the target and is.
    """
    if variant not in ("live", "installed"):
        raise ConfigError(f"unknown dracut variant {variant!r}; expected live or installed")
    return data_file("dracut", f"{'00-live' if variant == 'live' else '10-installed'}.conf")
