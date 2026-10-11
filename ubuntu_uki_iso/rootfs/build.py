"""Build live and installed Ubuntu rootfs trees and their SquashFS images."""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from .. import settings
from ..errors import BuildError, ConfigError
from ..shim import TARGET_BIN, TARGET_LIB, write_shim
from ..uki import config as boot
from . import config
from .folders import FolderLayout


def required_space_gb() -> int:
    """
    Return the minimum free disk space for a rootfs and its compressed image.
    """
    return 10


def preflight(temp_dir: Path) -> None:
    """
    Check dependencies, privileges and disk space before creating build files.

    Args:
        temp_dir: Directory whose filesystem will hold the rootfs artifacts.

    Returns:
        None. Raises BuildError if a prerequisite is missing.
    """
    packages = config.required_packages()
    query = subprocess.run(
        ["apt", "list", "--installed", *packages],
        capture_output=True,
        text=True,
        check=False,
    )
    if query.returncode:
        raise BuildError(f"apt could not inspect installed packages: {query.stderr.strip()}")
    installed = {line.partition("/")[0] for line in query.stdout.splitlines() if "/" in line}
    missing = [package for package in packages if package not in installed]
    if missing:
        raise BuildError(
            f"missing packages: {', '.join(missing)}\n"
            f"install with: apt-get install -y {' '.join(missing)}"
        )
    if os.geteuid() != 0:
        raise BuildError(
            "rootfs builds require root to preserve package ownership; run with sudo or in the build container"
        )
    probe = temp_dir.resolve()
    while not probe.exists() and probe.parent != probe:
        probe = probe.parent
    available = shutil.disk_usage(probe).free // (1024**3)
    if available < required_space_gb():
        raise BuildError(
            f"need about {required_space_gb()} GB free under {temp_dir}, have {available} GB"
        )
    print(f"disk: {available} GB available")


def copy_kernel_modules(kernel_deb: Path, root: Path, scratch: Path) -> str:
    """
    Extract matching modules into the rootfs without installing kernel packages.

    Args:
        kernel_deb: Built kernel image deb containing vmlinuz and its modules.
        root: Rootfs receiving the modules.
        scratch: Temporary directory used to unpack the kernel deb.

    Returns:
        Release derived from the kernel image inside the deb.
    """
    extracted = scratch / "kernel"
    subprocess.run(["dpkg-deb", "--extract", kernel_deb, extracted], check=True)
    kernels = sorted((extracted / "boot").glob("vmlinuz-*"))
    if len(kernels) != 1:
        raise BuildError("kernel deb must contain exactly one boot/vmlinuz-* image")
    release = kernels[0].name.removeprefix("vmlinuz-")
    modules = extracted / "lib/modules" / release
    if not modules.is_dir():
        modules = extracted / "usr/lib/modules" / release
    if not modules.is_dir():
        raise BuildError(f"kernel deb has no modules for {release}")
    destination = root / "usr/lib/modules" / release
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(modules, destination, symlinks=True)
    for name in ("build", "source"):
        (destination / name).unlink(missing_ok=True)
    subprocess.run(["depmod", "-b", root, release], check=True)
    metadata = root / "usr/lib/ubuntu-uki-iso"
    metadata.mkdir(parents=True, exist_ok=True)
    (metadata / "release").write_text(release + "\n", encoding="utf-8")
    return release


def customize(root: Path, mode: str) -> None:
    """
    Configure services and remove machine-specific state from a bootstrapped rootfs.

    Args:
        root: New rootfs tree, with package maintainer scripts already completed.
        mode: Image variant, either live or installed.

    Returns:
        None. Commands operate on the target tree without starting host services.
    """
    units = config.services(mode)
    print(f"==> customize ({mode})")
    hostname = f"{settings.BOOT_LABEL}-live" if mode == "live" else settings.BOOT_LABEL
    files = {
        "etc/hostname": hostname + "\n",
        "etc/hosts": f"127.0.0.1 localhost\n127.0.1.1 {hostname}\n::1 localhost ip6-localhost\n",
        "etc/kernel/cmdline": boot.cmdline(mode) + "\n",
        "etc/systemd/network/20-wired.network": "[Match]\nType=ether\n\n[Network]\nDHCP=yes\n",
    }
    if mode == "live":
        for unit in ("getty@tty1.service", "serial-getty@ttyS0.service"):
            files[f"etc/systemd/system/{unit}.d/autologin.conf"] = (
                "[Service]\nExecStart=\nExecStart=-/sbin/agetty --autologin root --noclear %I $TERM\n"
            )
    else:
        files["etc/systemd/system/ssh-host-keys.service"] = (
            "[Unit]\nDescription=Generate unique SSH host keys\nBefore=ssh.service\n"
            "[Service]\nType=oneshot\nExecStart=/usr/bin/ssh-keygen -A\nRemainAfterExit=yes\n"
            "[Install]\nWantedBy=multi-user.target\n"
        )
        files["etc/systemd/system/ssh.service.d/host-keys.conf"] = (
            "[Unit]\nRequires=ssh-host-keys.service\nAfter=ssh-host-keys.service\n"
        )
    for relative, content in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        path.chmod(0o644)
    if mode == "live":
        source = Path(__file__).resolve().parents[1]
        destination = root / TARGET_LIB.lstrip("/") / "ubuntu_uki_iso"
        destination.mkdir(parents=True)
        for name in ("installer", "uki"):
            shutil.copytree(
                source / name,
                destination / name,
                ignore=shutil.ignore_patterns("__pycache__", "build.py", "folders.py"),
            )
        for name in (
            "__init__.py",
            "settings.py",
            "errors.py",
            "log.py",
            "proc.py",
            "paths.py",
            "gpt.py",
            "pe.py",
            "media.py",
        ):
            shutil.copyfile(source / name, destination / name)
        write_shim(
            root / TARGET_BIN.lstrip("/"),
            "ubuntu_uki_iso.installer.cli",
            "Install the system payload from the live medium.",
        )
    subprocess.run(["systemctl", f"--root={root}", "enable", *units], check=True)
    for relative in ("etc/machine-id", "var/lib/dbus/machine-id"):
        path = root / relative
        path.unlink(missing_ok=True)
    (root / "etc/machine-id").write_text("", encoding="utf-8")
    for path in (root / "etc/ssh").glob("ssh_host_*_key*"):
        path.unlink()
    resolv = root / "etc/resolv.conf"
    resolv.unlink(missing_ok=True)
    resolv.symlink_to("../run/systemd/resolve/stub-resolv.conf")
    for relative in ("var/lib/apt/lists", "var/cache/apt/archives", "var/log", "tmp", "var/tmp"):
        directory = root / relative
        if directory.is_dir():
            for path in directory.iterdir():
                if path.is_dir() and not path.is_symlink():
                    shutil.rmtree(path)
                else:
                    path.unlink()


def squash(source: Path, destination: Path, jobs: int) -> Path:
    """
    Compress a rootfs, preserving ownership and publishing only a verified image.

    Args:
        source: Customized rootfs directory.
        destination: SquashFS image to create or replace.
        jobs: Number of compression worker threads.

    Returns:
        Path to the image after unsquashfs verifies its superblock.
    """
    if jobs < 1:
        raise BuildError("compression jobs must be positive")
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="squashfs-", dir=destination.parent) as scratch:
        candidate = Path(scratch) / "image.squashfs"
        argv = [
            "mksquashfs",
            str(source),
            str(candidate),
            "-comp",
            "zstd",
            "-Xcompression-level",
            str(config.compression_level()),
            "-processors",
            str(jobs),
            "-noappend",
            "-no-progress",
            "-wildcards",
        ]
        environment = os.environ.copy()
        epoch = environment.pop("SOURCE_DATE_EPOCH", None)
        if epoch:
            argv += ["-all-time", epoch, "-mkfs-time", epoch]
        argv += ["-e", *config.squashfs_excludes()]
        print(f"==> squashfs: {destination}")
        subprocess.run(argv, check=True, env=environment)
        subprocess.run(["unsquashfs", "-s", candidate], check=True)
        candidate.replace(destination)
    return destination


def build(kernel_deb: Path, temp_dir: Path, mode: str = "installed", jobs: int = 1) -> Path:
    """
    Bootstrap, customize and compress one Ubuntu rootfs using a built kernel deb.

    Args:
        kernel_deb: Kernel image deb whose modules are copied into the image.
        temp_dir: Directory holding kernel inputs and rootfs/SquashFS outputs.
        mode: Image variant, either live or installed.
        jobs: Number of SquashFS compression workers.

    Returns:
        Path to the generated SquashFS image. The uncompressed rootfs is retained.
    """
    packages = config.package_list(mode)
    if jobs < 1:
        raise BuildError("compression jobs must be positive")
    kernel_deb = kernel_deb.resolve()
    if not kernel_deb.is_file():
        raise BuildError(f"kernel package does not exist: {kernel_deb}")
    layout = FolderLayout(temp_dir)
    layout.require_dirs()
    with tempfile.TemporaryDirectory(prefix=f"rootfs-{mode}-", dir=layout.root) as scratch:
        stage = Path(scratch)
        root = stage / "rootfs"
        print(f"==> mmdebstrap ({mode})")
        subprocess.run(
            [
                "mmdebstrap",
                "--mode=root",
                "--format=directory",
                "--arch=amd64",
                "--variant=minbase",
                f"--components={settings.UBUNTU_COMPONENTS}",
                f"--include={packages.comma_joined()}",
                '--aptopt=Apt::Install-Recommends "false"',
                settings.UBUNTU_SUITE,
                str(root),
                settings.UBUNTU_MIRROR,
            ],
            check=True,
        )
        if not (root / "usr/lib/os-release").is_file():
            raise BuildError("mmdebstrap produced no Ubuntu os-release metadata")
        copy_kernel_modules(kernel_deb, root, stage)
        customize(root, mode)
        output = squash(root, layout.squashfs(mode), jobs)
        retained = layout.rootfs(mode)
        if retained.exists():
            shutil.rmtree(retained)
        root.replace(retained)
    return output


def main(argv: list[str] | None = None) -> int:
    """
    Parse arguments and build the selected rootfs images and their SquashFS artifacts.

    Args:
        argv: Command-line arguments, or None to use sys.argv[1:].

    Returns:
        Exit status: 0 on success or 1 on a handled build failure.
    """
    parser = argparse.ArgumentParser(description="Build Ubuntu rootfs and SquashFS images")
    parser.add_argument("--temp-dir", type=Path, required=True)
    parser.add_argument("--kernel-deb", type=Path, help="default: image deb in <temp-dir>/kernel")
    parser.add_argument("--mode", choices=("live", "installed", "all"), default="installed")
    parser.add_argument("--jobs", type=int, default=os.cpu_count() or 1)
    args = parser.parse_args(argv)
    if args.jobs < 1:
        parser.error("--jobs must be positive")
    try:
        preflight(args.temp_dir)
        layout = FolderLayout(args.temp_dir)
        image = args.kernel_deb or layout.kernel_image_deb()
        modes = ("live", "installed") if args.mode == "all" else (args.mode,)
        for mode in modes:
            output = build(image, args.temp_dir, mode, args.jobs)
            print(f"==> {mode}: done ({output})")
        return 0
    except (BuildError, ConfigError, OSError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    except subprocess.CalledProcessError as exc:
        command = subprocess.list2cmdline([str(arg) for arg in exc.cmd])
        print(f"ERROR: command failed ({exc.returncode}): {command}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
