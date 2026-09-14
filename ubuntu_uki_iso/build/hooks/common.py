"""Shared machinery for the mmdebstrap customize hooks.

mmdebstrap runs a ``--customize-hook`` on the *host* side, with the target
directory as its first argument and the chroot already set up with ``/proc``,
``/sys`` and ``/dev``. Commands meant for the new system therefore have to go
through :func:`chroot` explicitly — forgetting that is the mistake that
produces "command not found: systemctl" on a rootfs that has systemd in it.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

from ...errors import BuildError
from ...log import Console


def write_file(path: Path, content: str, mode: int = 0o644) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    path.chmod(mode)


def install_file(source: Path, destination: Path, mode: int = 0o644) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, destination)
    destination.chmod(mode)


def chroot(
    root: Path,
    *argv: str,
    env: dict[str, str] | None = None,
    check: bool = True,
    quiet: bool = False,
    capture: bool = False,
) -> subprocess.CompletedProcess:
    """Run a command inside the target.

    ``DEBIAN_FRONTEND=noninteractive`` is set for every call: a maintainer
    script that decides to ask a question would otherwise hang the build with
    no output and no indication of why.

    ``capture`` is for the few calls whose *output* is the answer rather than
    their exit code — apt's dependency resolution, for one. It merges stderr
    into stdout, because apt explains itself on stderr and the explanation is
    the part worth quoting back.
    """
    environment = {
        "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
        "DEBIAN_FRONTEND": "noninteractive",
        "LC_ALL": "C.UTF-8",
    }
    environment.update(env or {})

    if capture:
        stdout: int | None = subprocess.PIPE
    elif quiet:
        stdout = subprocess.DEVNULL
    else:
        stdout = None

    proc = subprocess.run(
        ["chroot", str(root), *argv],
        env=environment,
        check=False,
        stdout=stdout,
        stderr=subprocess.STDOUT if capture else None,
    )
    if check and proc.returncode != 0:
        raise BuildError(f"chroot {root}: {' '.join(argv)} failed ({proc.returncode})")
    return proc


def install_debs(root: Path, debs: list[Path]) -> None:
    """``dpkg -i`` a set of .debs into the target.

    The kernel postinst would normally call kernel-install, which with
    ``layout=uki`` runs dracut and ukify and wants a real ESP. There is no ESP
    during a rootfs build, so ``KERNEL_INSTALL_BOOT_ROOT`` is pointed at a
    scratch directory: that keeps the postinst happy and keeps the initramfs
    out of the way. The real UKI is built later, explicitly, where a failure is
    visible instead of buried in dpkg output.
    """
    staging = root / "tmp/kernel-debs"
    staging.mkdir(parents=True, exist_ok=True)
    for deb in debs:
        shutil.copyfile(deb, staging / deb.name)

    chroot(
        root,
        "env",
        "KERNEL_INSTALL_BOOT_ROOT=/var/tmp/kernel-install-scratch",
        "dpkg",
        "-i",
        *[f"/tmp/kernel-debs/{deb.name}" for deb in debs],
    )

    shutil.rmtree(staging, ignore_errors=True)
    shutil.rmtree(root / "var/tmp/kernel-install-scratch", ignore_errors=True)


def verify_kernel_installed(root: Path, release: str) -> None:
    """Check the parts that matter, rather than trusting dpkg's exit code.

    A missing ``/boot/vmlinuz-<rel>`` or ``/lib/modules/<rel>`` surfaces here,
    as a clear message, instead of twenty minutes later as an unbootable ISO.
    """
    if not (root / f"boot/vmlinuz-{release}").is_file():
        raise BuildError(
            f"the kernel installed but /boot/vmlinuz-{release} is missing from the rootfs"
        )
    if not (root / f"lib/modules/{release}").is_dir():
        raise BuildError(
            f"the kernel installed but /lib/modules/{release} is missing from the rootfs"
        )


def install_python_package(root: Path, console: Console) -> None:
    """Copy the ubuntu_uki_iso package and its console script into the target.

    The target rootfs is assembled from distribution packages by mmdebstrap,
    not by pip, so there is no console-script machinery to lean on. The package
    tree goes to ``/usr/local/lib/ubuntu-uki-iso`` and a shim goes to
    ``/usr/local/bin/ubuntu-uki-iso``; :mod:`ubuntu_uki_iso.shim` explains why.

    Note the two names, which are not interchangeable: the directory *holding*
    the package is named after the project and may contain hyphens, while the
    package directory inside it must be the importable module name. Naming both
    ``ubuntu-uki-iso`` produces a tree the shim cannot import from.
    """
    from ...shim import TARGET_BIN, TARGET_LIB, write_shim

    source = Path(__file__).resolve().parents[2]  # .../<repo>/ubuntu_uki_iso
    destination = root / TARGET_LIB.lstrip("/") / "ubuntu_uki_iso"
    if destination.exists():
        shutil.rmtree(destination)
    shutil.copytree(source, destination, ignore=shutil.ignore_patterns("__pycache__"))

    write_shim(
        root / TARGET_BIN.lstrip("/"),
        "ubuntu_uki_iso.cli",
        "ubuntu-uki-iso — build, verify and install the image.",
    )
    console.info("  installed the ubuntu-uki-iso package into the rootfs")


def cleanup(root: Path) -> None:
    """Drop package-manager scratch that is dead weight on the medium."""
    for path in (
        root / "var/lib/apt/lists",
        root / "var/cache/apt/archives",
    ):
        if path.is_dir():
            for entry in path.iterdir():
                if entry.name == "lock":
                    continue
                if entry.is_dir():
                    shutil.rmtree(entry, ignore_errors=True)
                else:
                    entry.unlink(missing_ok=True)


def target_argument(argv: list[str]) -> Path:
    """The target directory mmdebstrap passes as the hook's first argument."""
    if not argv:
        raise BuildError("the customize hook was called without a target directory")
    target = Path(argv[0])
    if not target.is_dir():
        raise BuildError(f"the customize hook target is not a directory: {target}")
    return target


def host_env() -> dict[str, str]:
    """Environment for a hook, kept minimal on purpose."""
    return {"LC_ALL": "C.UTF-8", "PYTHONPATH": os.environ.get("PYTHONPATH", "/work")}
