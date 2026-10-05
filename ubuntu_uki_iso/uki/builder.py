"""Build a UKI from a kernel deb without installing it on the host."""

from __future__ import annotations

import tempfile
from pathlib import Path

from .. import pe
from ..errors import BuildError
from ..proc import Runner
from . import config as boot


def preflight(runner: Runner) -> None:
    runner.require_packages(*boot.required_packages())
    runner.require("dpkg-deb", "dracut", "ukify", "depmod", "lsinitrd", "zstd")


def build(
    kernel_deb: Path,
    output: Path,
    runner: Runner,
    *,
    mode: str = "live",
    cmdline: str | None = None,
) -> Path:
    if mode not in ("live", "installed"):
        raise BuildError(f"unknown UKI mode: {mode}")
    kernel_deb, output = kernel_deb.resolve(), output.resolve()
    if not kernel_deb.is_file():
        raise BuildError(f"kernel package does not exist: {kernel_deb}")
    command_line = (
        cmdline
        if cmdline is not None
        else (boot.cmdline_live() if mode == "live" else boot.cmdline_installed())
    )
    if not command_line.strip():
        raise BuildError("kernel command line must not be empty")
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="uki-", dir=output.parent) as scratch:
        stage = Path(scratch)
        extracted = stage / "extract"
        runner.run("dpkg-deb", "-x", kernel_deb, extracted)
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
        runner.run("depmod", "-b", extracted, release)
        initrd = stage / "initramfs.img"
        # An empty confdir keeps the build host's dracut policy out of the image.
        confdir = stage / "conf.d"
        confdir.mkdir()
        conf = boot.dracut_conf("live" if mode == "live" else "uki-build").write(
            stage / "dracut.conf"
        )
        runner.run(
            "dracut",
            "--force",
            "--no-hostonly",
            "--no-hostonly-cmdline",
            f"--kver={release}",
            f"--kmoddir={modules}",
            f"--conf={conf}",
            f"--confdir={confdir}",
            initrd,
        )
        runner.probe("lsinitrd", initrd, check=True)
        candidate = stage / "uki.efi"
        runner.run(
            "ukify",
            "build",
            f"--linux={kernel}",
            f"--initrd={initrd}",
            f"--cmdline={command_line}",
            f"--uname={release}",
            "--os-release=@/etc/os-release",
            f"--output={candidate}",
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
