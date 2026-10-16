"""
Build NVIDIA open kernel modules offline and publish a kernel-specific deb.
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from .. import settings
from ..errors import BuildError
from ..media import digest
from .config import MODULES, PACKAGES, TOOLS


def read_headers(headers_deb: Path, destination: Path) -> tuple[Path, str]:
    """
    Extract an amd64 custom headers package and verify its generated build files.
    """
    metadata = subprocess.run(
        ["dpkg-deb", "-f", headers_deb, "Package", "Architecture"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    fields = dict(line.split(": ", 1) for line in metadata.splitlines() if ": " in line)
    name = fields.get("Package", "")
    release = name.removeprefix("linux-headers-")
    if not name.startswith("linux-headers-") or not re.fullmatch(r"[a-z0-9][a-z0-9.+-]*", release):
        raise BuildError("expected a custom linux-headers-<release> package")
    if fields.get("Architecture") != settings.DPKG_ARCH:
        raise BuildError(f"headers must target {settings.DPKG_ARCH}")
    subprocess.run(["dpkg-deb", "-x", headers_deb, destination], check=True)
    headers = destination / "usr/src" / f"linux-headers-{release}"
    for name in (
        "Makefile",
        "include/config/auto.conf",
        "Module.symvers",
        "include/generated/autoconf.h",
        "include/config/kernel.release",
    ):
        if not (headers / name).is_file():
            raise BuildError(f"headers package is missing {name}")
    if (headers / "include/config/kernel.release").read_text().strip() != release:
        raise BuildError("headers package and embedded kernel release differ")
    return headers, release


def verify_modules(source: Path, release: str, version: str) -> list[Path]:
    """
    Require all selected modules to match the requested kernel and driver version.
    """
    modules = [source / "kernel-open" / f"{name}.ko" for name in MODULES]
    for module in modules:
        if not module.is_file():
            raise BuildError(f"NVIDIA build did not produce {module.name}")
        for field, expected in (
            ("vermagic", release),
            ("version", version),
            ("name", module.stem.replace("-", "_")),
        ):
            value = subprocess.run(
                ["modinfo", "-F", field, module],
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            if (value.split()[0] if value else "") != expected:
                raise BuildError(f"{module.name}: {field} does not match {expected}")
    return modules


def package(
    modules: list[Path], source: Path, release: str, version: str, headers_hash: str, output: Path
) -> Path:
    """
    Publish a runtime-only deb with kernel dependencies and depmod maintainer hooks.
    """
    name = f"nvidia-modules-{release}"
    with tempfile.TemporaryDirectory(prefix="nvidia-deb-", dir=output.parent) as scratch:
        tree = Path(scratch) / "root"
        control = tree / "DEBIAN"
        control.mkdir(parents=True)
        (control / "control").write_text(
            f"Package: {name}\nVersion: {version}-1\nArchitecture: {settings.DPKG_ARCH}\n"
            "Maintainer: Ubuntu UKI ISO @xiahualiu\n"
            f"Depends: kmod, ubuntu-uki-boot-installed (= 1.0-{release}) | ubuntu-uki-boot-live (= 1.0-{release})\n"
            f"Description: NVIDIA {version} open modules for Linux {release}\n"
            " Prebuilt runtime modules; no DKMS, compiler or headers required.\n"
        )
        for script, action in (("postinst", "configure"), ("postrm", "remove")):
            path = control / script
            path.write_text(
                f'#!/bin/sh\nset -eu\nif [ "$1" = {action} ]; then\n    depmod -a {release}\nfi\n'
            )
            path.chmod(0o755)
        destination = tree / "usr/lib/modules" / release / "updates/nvidia"
        destination.mkdir(parents=True)
        for module in modules:
            shutil.copyfile(module, destination / module.name)
        docs = tree / "usr/share/doc" / name
        docs.mkdir(parents=True)
        shutil.copyfile(source / "COPYING", docs / "copyright")
        (docs / "build.json").write_text(
            json.dumps(
                {
                    "kernel_release": release,
                    "driver_version": version,
                    "headers_sha256": headers_hash,
                    "flavor": "open",
                },
                indent=2,
            )
            + "\n"
        )
        candidate = Path(scratch) / "modules.deb"
        subprocess.run(["dpkg-deb", "--root-owner-group", "--build", tree, candidate], check=True)
        candidate.replace(output)
    return output


def build(
    headers_deb: Path,
    source_dir: Path,
    temp_dir: Path,
    *,
    jobs: int = 1,
    driver_version: str | None = None,
) -> Path:
    """
    Build an explicit local NVIDIA source tree against an explicit headers deb.

    Nothing is downloaded, installed on the host, or loaded into the running kernel.
    Source and headers are copied into scratch space; only the verified deb is published.
    """
    if jobs < 1:
        raise BuildError("jobs must be positive")
    missing = [tool for tool in TOOLS if not shutil.which(tool)]
    if missing:
        raise BuildError(f"missing tools: {', '.join(missing)}; install: {' '.join(PACKAGES)}")
    headers_deb, source_dir = headers_deb.resolve(), source_dir.resolve()
    if not headers_deb.is_file():
        raise BuildError(f"headers package does not exist: {headers_deb}")
    for name in ("version.mk", "Makefile", "kernel-open/Makefile", "COPYING"):
        if not (source_dir / name).is_file():
            raise BuildError(f"NVIDIA open source tree is missing {name}")
    match = re.search(
        r"^NVIDIA_VERSION\s*=\s*([0-9]+(?:\.[0-9]+)+)\s*$",
        (source_dir / "version.mk").read_text(),
        re.M,
    )
    if not match:
        raise BuildError("cannot read NVIDIA_VERSION from version.mk")
    version = match[1]
    if driver_version is not None and driver_version != version:
        raise BuildError(f"requested NVIDIA {driver_version}, but source contains {version}")
    output_dir = temp_dir.resolve() / "nvidia"
    if source_dir == output_dir or source_dir in output_dir.parents:
        raise BuildError("build directory must be outside the NVIDIA source tree")
    output_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="build-", dir=output_dir) as scratch:
        stage = Path(scratch)
        headers, release = read_headers(headers_deb, stage / "headers")
        source = stage / "source"
        shutil.copytree(source_dir, source, symlinks=True, ignore=shutil.ignore_patterns(".git"))
        environment = os.environ.copy()
        for key in ("KERNELRELEASE", "KBUILD_OUTPUT", "KBUILD_EXTMOD"):
            environment.pop(key, None)
        subprocess.run(
            [
                "make",
                "-C",
                source,
                f"-j{jobs}",
                "modules",
                f"SYSSRC={headers}",
                f"SYSOUT={headers}",
                f"KERNEL_UNAME={release}",
                "CC=gcc",
                "CXX=g++",
                "ARCH=x86_64",
                "TARGET_ARCH=x86_64",
                f"NV_KERNEL_MODULES={' '.join(MODULES)}",
            ],
            check=True,
            env=environment,
        )
        modules = verify_modules(source, release, version)
        output = output_dir / f"nvidia-modules-{release}_{version}-1_{settings.DPKG_ARCH}.deb"
        return package(modules, source, release, version, digest(headers_deb), output)


def main(argv: list[str] | None = None) -> int:
    """
    Parse explicit offline inputs and build the kernel-specific NVIDIA deb.
    """
    parser = argparse.ArgumentParser(description="Build NVIDIA open kernel modules offline")
    parser.add_argument("--temp-dir", type=Path, required=True)
    parser.add_argument("--headers-deb", type=Path, required=True)
    parser.add_argument(
        "--source-dir",
        type=Path,
        required=True,
        help="local NVIDIA open-gpu-kernel-modules release source",
    )
    parser.add_argument(
        "--driver-version",
        help="exact NVIDIA release, e.g. 595.80; defaults to the local source version",
    )
    parser.add_argument("--jobs", type=int, default=min(os.cpu_count() or 1, 16))
    args = parser.parse_args(argv)
    try:
        print(
            build(
                args.headers_deb,
                args.source_dir,
                args.temp_dir,
                jobs=args.jobs,
                driver_version=args.driver_version,
            )
        )
        return 0
    except (BuildError, OSError, subprocess.SubprocessError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
