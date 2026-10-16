"""
Generate the installed boot package's firmware-registration maintainer script.
"""

import shlex

from .. import settings


def render() -> str:
    """
    Register the packaged fallback loader when explicitly requested by the installer.

    Rootfs construction and ordinary package reconfiguration leave firmware alone.
    Existing entries with our exact label are left alone. New entries are created
    without changing BootOrder or BootNext.
    """
    label = shlex.quote(settings.BOOT_LABEL)
    return (
        """#!/bin/sh
set -eu
[ "${1:-}" = configure ] || exit 0
[ "${UBUNTU_UKI_REGISTER_EFI:-0}" = 1 ] || exit 0
: "${UBUNTU_UKI_EFI_DISK:?installer must supply the target disk}"
: "${UBUNTU_UKI_EFI_PART:?installer must supply the ESP partition number}"
mountpoint -q /boot/efi || { echo "ESP is not mounted" >&2; exit 1; }
mountpoint -q /sys/firmware/efi/efivars || { echo "EFI variables are not mounted" >&2; exit 1; }
label="""
        + label
        + """
entries=$(efibootmgr)
existing=$(printf '%s\\n' "$entries" | awk -v label="$label" '
    /^Boot[[:xdigit:]][[:xdigit:]][[:xdigit:]][[:xdigit:]][* ]/ {
        name = $0
        sub(/^Boot[[:xdigit:]]+[ *]+/, "", name)
        sub(/\t.*/, "", name)
        if (name == label) print substr($0, 5, 4)
    }
')
[ -z "$existing" ] || exit 0
efibootmgr --create-only --disk "$UBUNTU_UKI_EFI_DISK" --part "$UBUNTU_UKI_EFI_PART" \\
    --label "$label" --loader '\\EFI\\BOOT\\BOOTX64.EFI'
"""
    )
