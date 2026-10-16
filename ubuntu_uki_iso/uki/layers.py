"""
Mount the live-only SquashFS before dracut mounts the writable root overlay.
"""

from pathlib import Path

HOOK = """#!/bin/sh
# dmsquash-live prepares rootfsbase during initqueue, before pre-mount hooks.
[ -d /run/rootfsbase ] || { die "Shared live base is not mounted"; return 1; }
[ -e /run/initramfs/live-layer-ready ] && return 0
mkdir -p /run/initramfs/installer /run/initramfs/shared-base
# dracut has detached its temporary squashfs mount; retain the bound base.
mount --bind /run/rootfsbase /run/initramfs/shared-base || {
    die "Cannot retain the shared base"; return 1;
}
mount -t squashfs -o ro,loop /run/initramfs/live/live/live-overlay.squashfs /run/initramfs/installer || {
    die "Cannot mount the live installer layer"; return 1;
}
# This read-only union becomes the lower filesystem of dracut's RAM overlay.
mount -t overlay -o ro,lowerdir=/run/initramfs/installer:/run/initramfs/shared-base live-base /run/rootfsbase || {
    die "Cannot combine the live installer and shared base"; return 1;
}
: > /run/initramfs/live-layer-ready
"""


def write_hook(directory: Path) -> Path:
    """
    Write the executable pre-mount hook for inclusion in the live initramfs.
    """
    path = directory / "live-layer.sh"
    path.write_text(HOOK)
    path.chmod(0o755)
    return path
