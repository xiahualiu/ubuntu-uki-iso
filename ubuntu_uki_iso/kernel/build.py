"""
Build Ubuntu kernel packages under the required temporary directory.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tarfile
import time
from pathlib import Path

from ..errors import BuildError
from . import config as kconfig
from .folders import FolderLayout


def required_space_gb() -> int:
    """
    Free disk space required for a kernel build.

    Returns:
        Minimum free disk space in gigabytes.
    """
    return 30


def download_package(package: str, destination: Path) -> Path:
    """
    Download one apt package into a task-specific directory.

    Args:
        package: Apt package name to download.
        destination: Directory where apt downloads the package.

    Returns:
        Path to the downloaded Debian package.
    """
    destination.mkdir(parents=True, exist_ok=True)
    print(f"==> apt-get download {package}")
    proc = subprocess.run(
        ["apt-get", "download", package],
        cwd=destination,
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise BuildError(
            f"could not download {package}:\n"
            f"{proc.stderr.strip() or proc.stdout.strip() or '(no output)'}"
        )
    debs = list(destination.glob(f"{package}_*.deb"))
    if len(debs) > 1:
        raise BuildError(f"expected one downloaded package for {package}, found {len(debs)}")
    if not debs:
        raise BuildError(f"apt reported success but {package} produced no .deb in {destination}")
    return debs[0]


def unpack_source(tarball: Path, destination: Path) -> Path:
    """
    Extract the .tar.bz2 source archive into its destination.

    The local Ubuntu 7.0.0 source package contains
    linux-source-7.0.0.tar.bz2 with a linux-source-7.0.0 top-level
    directory.

    Args:
        tarball: Compressed archive containing the kernel source tree.
        destination: Parent directory for the extracted source tree.

    Returns:
        Source-tree directory, reusing an existing tree when its Makefile exists.
    """
    with tarfile.open(tarball, "r:bz2") as archive:
        archive.extractall(destination, filter="data")
    return destination / tarball.name.removesuffix(".tar.bz2")


def fetch_source(download_dir: Path) -> Path:
    """
    Download and unpack the Ubuntu source package.

    Args:
        download_dir: Directory for the downloaded package and extracted sources.

    Returns:
        Path to the extracted kernel source tree.
    """
    print(f"==> fetch source ({kconfig.KERNEL_SOURCE_PKG})")
    download_dir.mkdir(parents=True, exist_ok=True)
    deb = download_package(kconfig.KERNEL_SOURCE_PKG, download_dir)
    print(f"extracting {deb.name} ...")
    extracted = download_dir / "package"
    subprocess.run(["dpkg-deb", "--extract", str(deb), str(extracted)], check=True)
    tarballs = sorted(
        {
            path.resolve()
            for path in (extracted / "usr/src").rglob("linux-source-*.tar.bz2")
            if path.is_file()
        }
    )
    if len(tarballs) != 1:
        raise BuildError(f"expected one source tarball inside {deb.name}, found {len(tarballs)}")
    tarball = tarballs[0]
    print(f"unpacking {tarball.name} ...")
    return unpack_source(tarball, download_dir)


def configure(source_dir: Path) -> Path:
    """
    Generate the kernel configuration from x86-64 defaults and project overrides.

    Args:
        source_dir: Extracted kernel source tree to configure.

    Returns:
        Path to the resolved .config in the source tree, with build settings synchronized.
    """
    # Reset .config on every build so previous configuration changes cannot carry over.
    print("==> generate base config (x86_64_defconfig)")
    subprocess.run(["make", "ARCH=x86", "x86_64_defconfig"], cwd=source_dir, check=True)
    config = source_dir / ".config"

    print("==> apply config override")
    config_override = source_dir / "config-trim.override"
    config_override.write_text(kconfig.render_config_override(), encoding="utf-8")

    # Merge overrides directly into the generated .config. -m skips the script's
    # make step; resolve dependencies explicitly below. Run in the source tree
    # so the script and make read the same .config.
    subprocess.run(
        [
            str(source_dir / "scripts/kconfig/merge_config.sh"),
            "-m",
            ".config",
            str(config_override),
        ],
        check=True,
        cwd=source_dir,
    )
    # Fill unspecified options with defaults and resolve Kconfig dependencies.
    subprocess.run(["make", "olddefconfig"], check=True, cwd=source_dir)

    # Refresh generated build settings, including include/config/auto.conf.
    # kernelrelease does not refresh it itself, so a reused source tree could
    # otherwise report a stale CONFIG_LOCALVERSION.
    subprocess.run(["make", "syncconfig"], check=True, cwd=source_dir)

    return config


def kernel_release(source: Path) -> str:
    """
    Read the configured kernel release from the source tree.

    Args:
        source: Configured kernel source tree where make runs.

    Returns:
        Kernel release string reported by make kernelrelease.
    """
    # Runs in the source tree; `make` reads the .config from its working
    # directory, so there is no version of this that works from elsewhere.
    proc = subprocess.run(
        ["make", "-s", "kernelrelease"],
        cwd=source,
        capture_output=True,
        text=True,
        check=True,
    )
    release = proc.stdout.strip()

    if not release:
        raise BuildError("make kernelrelease returned nothing")
    return release


def image_package(release: str) -> str:
    """
    Return the full kernel image package name for the release.
    """
    return f"linux-image-{release}"


def headers_package(release: str) -> str:
    """
    Return the full kernel headers package name for the release.
    """
    return f"linux-headers-{release}"


def build_packages(source_dir: Path, jobs: int, out_dir: Path, release: str) -> None:
    """
    Compile Debian kernel packages and copy them into the artifact directory.

    Args:
        source_dir: Configured kernel source tree.
        jobs: Number of parallel make jobs.
        out_dir: Directory for copied kernel packages and the build log.
        release: Configured kernel release used to identify image and headers packages.

    """

    out_dir.mkdir(parents=True, exist_ok=True)
    print(f"==> build (make bindeb-pkg -j{jobs})")
    print(f"watching: {out_dir / 'build.log'}")

    started = time.monotonic()
    with (out_dir / "build.log").open("a", encoding="utf-8") as log:
        subprocess.run(
            ["make", "bindeb-pkg", f"-j{jobs}"],
            check=True,
            stdout=log,
            stderr=subprocess.STDOUT,
            cwd=source_dir,
        )
    print(f"built in {int(time.monotonic() - started) // 60} min")

    # debian/files records the exact output filenames, including package versions.
    manifest = source_dir / "debian/files"
    if not manifest.is_file():
        raise BuildError("the build produced no debian/files package manifest")
    filenames = [line.split()[0] for line in manifest.read_text().splitlines() if line.strip()]
    packages: list[Path] = []
    for package_name in (image_package(release), headers_package(release)):
        matches = [
            source_dir.parent / filename
            for filename in filenames
            if filename.endswith(".deb") and filename.partition("_")[0] == package_name
        ]
        if len(matches) != 1:
            raise BuildError(f"expected one {package_name} package, found {len(matches)}")
        if not matches[0].is_file():
            raise BuildError(f"package listed in debian/files is missing: {matches[0].name}")
        packages.append(matches[0])

    for deb in packages:
        shutil.copyfile(deb, out_dir / deb.name)
        print(f"packaged: {deb.name}")


# ---------------------------------------------------------------------------
# entry points
# ---------------------------------------------------------------------------


def preflight(temp_dir: Path) -> None:
    """
    Check kernel prerequisites before downloading or changing files.

    Args:
        temp_dir: Temporary build directory used to locate the filesystem for the space check.

    Returns:
        None. Raises BuildError if required packages or disk space are missing.
    """
    packages = kconfig.required_packages()
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
        raise BuildError("kernel build preflight failed\n" + "\n".join(details))

    temp_dir = temp_dir.resolve()
    probe = temp_dir
    while not probe.exists() and probe.parent != probe:
        probe = probe.parent
    available_gb = shutil.disk_usage(probe).free // (1024**3)
    needed_gb = required_space_gb()
    if available_gb < needed_gb:
        raise BuildError(
            f"need about {needed_gb} GB free under {temp_dir}, have {available_gb} GB.\n"
            "A kernel build with debug info is not small."
        )
    print(f"disk: {available_gb} GB available")


def main(argv: list[str] | None = None) -> int:
    """
    Parse command-line arguments and run the kernel build tasks.

    Args:
        argv: Command-line arguments, or None to use sys.argv[1:].

    Returns:
        Exit status: 0 on success or 1 on a handled build failure.
    """
    import argparse

    parser = argparse.ArgumentParser(description="Build Ubuntu kernel Debian packages")
    parser.add_argument("--jobs", type=int, default=os.cpu_count() or 1)
    parser.add_argument(
        "--temp-dir",
        type=Path,
        required=True,
        help="directory for temporary kernel sources and build files",
    )
    args = parser.parse_args(argv)
    if args.jobs < 1:
        parser.error("--jobs must be positive")
    try:
        layout = FolderLayout(args.temp_dir)

        preflight(args.temp_dir)
        layout.require_dirs()
        source = fetch_source(layout.download_dir)
        configure(source)

        release = kernel_release(source)
        (layout.kernel / "release").write_text(release + "\n", encoding="utf-8")
        print(f"kernel release: {release}")

        build_packages(source, args.jobs, layout.kernel, release)

        print("==> kernel: done")
        print(f"release: {release}")
        for deb in sorted(layout.kernel.glob("*.deb")):
            print(f"  {deb.name}  {deb.stat().st_size // (1024 * 1024)} MB")
        return 0
    except BuildError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    except subprocess.CalledProcessError as exc:
        command = subprocess.list2cmdline([str(arg) for arg in exc.cmd])
        print(f"ERROR: command failed ({exc.returncode}): {command}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
