"""
Package a built UKI and matching modules as a Debian boot package.
"""

import shutil
import subprocess
import tempfile
from pathlib import Path

from .. import media, settings
from ..boot_package import EFI_PATH, METADATA
from ..errors import BuildError
from ..settings import Mode
from . import postinst


def create_package(uki: Path, modules: Path, release: str, mode: Mode, output: Path) -> Path:
    """
    Publish a boot package; installed mode registers EFI through its postinst.
    """
    if media.validate_uki(uki, mode) != release or modules.name != release:
        raise BuildError("boot package kernel release mismatch")
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="boot-deb-", dir=output.parent) as scratch:
        tree = Path(scratch) / "root"
        (tree / "DEBIAN").mkdir(parents=True)
        (tree / "DEBIAN/control").write_text(
            f"Package: ubuntu-uki-boot-{mode}\nVersion: 1.0-{release}\n"
            f"Architecture: {settings.DPKG_ARCH}\nMaintainer: Ubuntu UKI ISO builders <root@localhost>\n"
            f"Depends: {'kmod, efibootmgr, util-linux' if mode == Mode.INSTALLED else 'kmod'}\n"
            f"Conflicts: ubuntu-uki-boot-{Mode.INSTALLED if mode == Mode.LIVE else Mode.LIVE}\n"
            f"Description: {mode} UKI and matching Linux runtime modules\n"
        )
        if mode == Mode.INSTALLED:
            script = tree / "DEBIAN/postinst"
            script.write_text(postinst.render(), encoding="utf-8")
            script.chmod(0o755)
        target = tree / "usr/lib/modules" / release
        target.parent.mkdir(parents=True)
        shutil.copytree(modules, target, symlinks=True)
        for name in ("build", "source"):
            (target / name).unlink(missing_ok=True)
        (tree / EFI_PATH).parent.mkdir(parents=True)
        shutil.copyfile(uki, tree / EFI_PATH)
        (tree / METADATA).mkdir(parents=True)
        (tree / METADATA / "release").write_text(release + "\n")
        (tree / METADATA / "mode").write_text(mode + "\n")
        candidate = Path(scratch) / "boot.deb"
        subprocess.run(["dpkg-deb", "--root-owner-group", "--build", tree, candidate], check=True)
        candidate.replace(output)
    return output
