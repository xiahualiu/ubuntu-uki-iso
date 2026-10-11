"""Build a UKI EFI from a kernel deb, with a standalone command entry point."""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from .. import pe
from ..errors import BuildError, ConfigError
from . import config as boot
from .folders import FolderLayout


def required_space_gb() -> int:
    """
    Free disk space required for a UKI build.

    Returns:
        Minimum free disk space in gigabytes.
    """
    return 2


def preflight(temp_dir: Path) -> None:
    """
    Check UKI prerequisites before extracting the kernel or changing files.

    Args:
        temp_dir: Temporary build directory used to locate the filesystem for the space check.

    Returns:
        None. Raises BuildError if required packages or disk space are missing.
    """
    packages = boot.required_packages()
    query = subprocess.run(
        ["apt", "list", "--installed", *packages],
        capture_output=True,
        text=True,
        check=False,
    )
    if query.returncode != 0:
        raise BuildError(f"apt could not inspect installed packages: {query.stderr.strip()}")
    installed = {
        line.partition("/")[0]
        for line in query.stdout.splitlines()
        if "/" in line and not line.startswith("Listing")
    }
    missing_packages = [package for package in packages if package not in installed]
    if missing_packages:
        details = [
            f"missing packages: {', '.join(missing_packages)}",
            f"install with: apt-get install -y {' '.join(missing_packages)}",
        ]
        raise BuildError("UKI build preflight failed\n" + "\n".join(details))

    probe = temp_dir.resolve()
    while not probe.exists() and probe.parent != probe:
        probe = probe.parent
    available_gb = shutil.disk_usage(probe).free // (1024**3)
    if available_gb < required_space_gb():
        raise BuildError(
            f"need about {required_space_gb()} GB free under {temp_dir}, have {available_gb} GB"
        )

    print(f"disk: {available_gb} GB available")


def build(
    kernel_deb: Path,
    output: Path,
    mode: str = "installed",
) -> Path:
    """
    Build and verify a UKI EFI image from an extracted kernel Debian package.

    Args:
        kernel_deb: Kernel image package containing the kernel and matching modules.
        output: Destination for the EFI image and adjacent initramfs artifact.
        mode: Boot configuration variant, either live or installed.

    Returns:
        Resolved path to the verified EFI image. The initramfs is retained beside it
        with the .initramfs.img suffix.
    """
    if mode not in ("live", "installed"):
        raise BuildError(f"unknown UKI mode: {mode}")
    kernel_deb, output = kernel_deb.resolve(), output.resolve()
    if not kernel_deb.is_file():
        raise BuildError(f"kernel package does not exist: {kernel_deb}")
    command_line = boot.cmdline(mode)
    if not command_line.strip():
        raise BuildError("kernel command line must not be empty")
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="uki-", dir=output.parent) as scratch:
        stage = Path(scratch)
        extracted = stage / "extract"
        print(f"==> extract {kernel_deb.name}")
        subprocess.run(["dpkg-deb", "-x", kernel_deb, extracted], check=True)
        kernels = sorted((extracted / "boot").glob("vmlinuz-*"))
        if len(kernels) != 1:
            raise BuildError("kernel deb must contain exactly one boot/vmlinuz-* image")
        kernel = kernels[0]
        release = kernel.name.removeprefix("vmlinuz-")
        modules = extracted / "lib/modules" / release
        if not modules.is_dir():
            modules = extracted / "usr/lib/modules" / release
        if not modules.is_dir():
            raise BuildError(f"kernel deb has no modules for {release}")
        subprocess.run(["depmod", "-b", extracted, release], check=True)
        initrd = stage / "initramfs.img"
        # An empty confdir excludes /etc/dracut.conf.d; vendor drop-ins still apply.
        confdir = stage / "conf.d"
        confdir.mkdir()
        conf = boot.dracut_conf("live" if mode == "live" else "uki-build").write(
            stage / "dracut.conf"
        )
        print(f"==> dracut ({release})")
        subprocess.run(
            [
                "dracut",
                "--force",
                "--no-hostonly",
                "--no-hostonly-cmdline",
                f"--kver={release}",
                f"--kmoddir={modules}",
                f"--conf={conf}",
                f"--confdir={confdir}",
                f"--tmpdir={stage}",
                initrd,
            ],
            check=True,
        )
        subprocess.run(["lsinitrd", initrd], check=True)
        candidate = stage / "uki.efi"
        print("==> ukify")
        subprocess.run(
            [
                "ukify",
                "build",
                f"--linux={kernel}",
                f"--initrd={initrd}",
                f"--cmdline={command_line}",
                f"--uname={release}",
                "--os-release=@/etc/os-release",
                f"--output={candidate}",
            ],
            check=True,
        )
        for section in (".linux", ".initrd", ".cmdline"):
            if not pe.read_section(candidate, section):
                raise BuildError(f"UKI is missing {section}")
        if pe.cmdline(candidate) != command_line:
            raise BuildError("UKI embedded command line differs from requested command line")
        candidate.replace(output)
        # Keep the initramfs as an independently inspectable artifact.
        initrd.replace(output.with_suffix(".initramfs.img"))
    return output


def main(argv: list[str] | None = None) -> int:
    """
    Parse command-line arguments and run the UKI build tasks.

    Args:
        argv: Command-line arguments, or None to use sys.argv[1:].

    Returns:
        Exit status: 0 on success or 1 on a handled build failure.
    """
    parser = argparse.ArgumentParser(description="Build a UKI EFI file from a kernel deb")
    parser.add_argument(
        "--temp-dir",
        type=Path,
        required=True,
        help="directory holding kernel/ inputs and uki/ build outputs",
    )
    parser.add_argument("--kernel-deb", type=Path, help="default: image deb in <temp-dir>/kernel")
    parser.add_argument("--mode", choices=("live", "installed"), default="installed")
    args = parser.parse_args(argv)
    try:
        layout = FolderLayout(args.temp_dir)
        preflight(args.temp_dir)
        image = args.kernel_deb or layout.kernel_image_deb()
        layout.require_dirs()
        output = layout.uki / ("BOOTX64.EFI" if args.mode == "live" else "uki.efi")
        build(image, output, mode=args.mode)
        print("==> UKI: done")
        print(f"UKI: {output}")
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
