"""
Create a read-only OverlayFS layer from the differences between two trees.
"""

import filecmp
import os
import shutil
import stat
from pathlib import Path


def difference(base: Path, live: Path, layer: Path) -> None:
    """
    Copy changed files and encode removals as OverlayFS whiteout devices.

    Ownership, permissions, symlinks and extended attributes are preserved.
    Directory timestamps do not cause otherwise identical contents to be copied.
    """
    layer.mkdir()
    for name in sorted({p.name for p in base.iterdir()} | {p.name for p in live.iterdir()}):
        old, new, target = base / name, live / name, layer / name
        if not new.exists() and not new.is_symlink():
            os.mknod(target, stat.S_IFCHR | 0o600, os.makedev(0, 0))
            continue
        previous = old.lstat() if old.exists() or old.is_symlink() else None
        current = new.lstat()
        if stat.S_ISDIR(current.st_mode):
            if previous and stat.S_ISDIR(previous.st_mode):
                difference(old, new, target)
            else:
                shutil.copytree(new, target, symlinks=True)
        else:
            same = previous and (previous.st_mode, previous.st_uid, previous.st_gid) == (
                current.st_mode,
                current.st_uid,
                current.st_gid,
            )
            if same and new.is_symlink():
                same = old.readlink() == new.readlink()
            elif same and stat.S_ISREG(current.st_mode):
                same = filecmp.cmp(old, new, shallow=False)
            else:
                same = False
            if same:
                same = os.listxattr(old, follow_symlinks=False) == os.listxattr(
                    new, follow_symlinks=False
                ) and all(
                    os.getxattr(old, key, follow_symlinks=False)
                    == os.getxattr(new, key, follow_symlinks=False)
                    for key in os.listxattr(new, follow_symlinks=False)
                )
            if same:
                continue
            if stat.S_ISREG(current.st_mode) or new.is_symlink():
                shutil.copy2(new, target, follow_symlinks=False)
            else:
                os.mknod(target, current.st_mode, current.st_rdev)
                shutil.copystat(new, target, follow_symlinks=False)
        os.chown(target, current.st_uid, current.st_gid, follow_symlinks=False)
    shutil.copystat(live, layer)
    metadata = live.stat()
    os.chown(layer, metadata.st_uid, metadata.st_gid)
