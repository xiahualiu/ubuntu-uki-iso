"""
Offline NVIDIA builds bind modules to explicit headers and package only runtime files.
"""

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import Mock

import pytest

from ubuntu_uki_iso.errors import BuildError
from ubuntu_uki_iso.nvidia import build
from ubuntu_uki_iso.nvidia.config import MODULES


def source_tree(path: Path) -> Path:
    for name in ("Makefile", "kernel-open/Makefile", "COPYING"):
        file = path / name
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text("source")
    (path / "version.mk").write_text("NVIDIA_VERSION = 615.78.08\n")
    return path


def headers_tree(destination: Path, release: str = "7.0-test") -> Path:
    headers = destination / "usr/src/linux-headers-7.0-test"
    for name in (
        "Makefile",
        "include/config/auto.conf",
        "Module.symvers",
        "include/generated/autoconf.h",
        "include/config/kernel.release",
    ):
        path = headers / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(release if name.endswith("kernel.release") else "header")
    return headers


@pytest.mark.parametrize(
    "package,arch", [("linux-image-7.0-test", "amd64"), ("linux-headers-7.0-test", "arm64")]
)
def test_headers_rejected_before_extraction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, package: str, arch: str
) -> None:
    run = Mock(
        return_value=subprocess.CompletedProcess(
            [], 0, f"Package: {package}\nArchitecture: {arch}\n"
        )
    )
    monkeypatch.setattr(build.subprocess, "run", run)
    with pytest.raises(BuildError):
        build.read_headers(tmp_path / "headers.deb", tmp_path / "extract")
    assert run.call_count == 1


def test_headers_embedded_release_must_match_package(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def run(args: list[Any], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        if "-x" in args:
            headers_tree(args[-1], "wrong")
        return subprocess.CompletedProcess(
            args, 0, "Package: linux-headers-7.0-test\nArchitecture: amd64\n"
        )

    monkeypatch.setattr(build.subprocess, "run", run)
    with pytest.raises(BuildError, match="embedded kernel release"):
        build.read_headers(tmp_path / "headers.deb", tmp_path / "extract")


@pytest.mark.parametrize("bad_field", ["vermagic", "version", "name"])
def test_module_metadata_is_checked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, bad_field: str
) -> None:
    source = source_tree(tmp_path / "source")
    for name in MODULES:
        (source / "kernel-open" / f"{name}.ko").touch()

    def run(args: list[Any], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        field, module = args[2:]
        value = {
            "vermagic": "7.0-test SMP",
            "version": "615.78.08",
            "name": module.stem.replace("-", "_"),
        }[field]
        return subprocess.CompletedProcess(args, 0, "wrong" if field == bad_field else value)

    monkeypatch.setattr(build.subprocess, "run", run)
    with pytest.raises(BuildError, match=bad_field):
        build.verify_modules(source, "7.0-test", "615.78.08")


@pytest.mark.parametrize("version", ["595.80", "615.78.08"])
@pytest.mark.parametrize("wrong_kernel", [False, True])
def test_offline_build_uses_copied_sources_and_explicit_kernel(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, wrong_kernel: bool, version: str
) -> None:
    source = source_tree(tmp_path / "source")
    (source / "version.mk").write_text(f"NVIDIA_VERSION = {version}\n")
    headers = tmp_path / "headers.deb"
    headers.write_bytes(b"headers")
    commands = []
    output = tmp_path / f"out/nvidia/nvidia-modules-7.0-test_{version}-1_amd64.deb"
    output.parent.mkdir(parents=True)
    output.write_bytes(b"previous")
    monkeypatch.setattr(build.shutil, "which", lambda name: name)

    def run(args: list[Any], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        commands.append(args)
        if args[0] == "dpkg-deb" and "-x" in args:
            headers_tree(args[-1])
        if args[0] == "make":
            for name in MODULES:
                (args[2] / "kernel-open" / f"{name}.ko").write_bytes(b"module")
        if args[0] == "modinfo":
            field, module = args[2:]
            value = {
                "vermagic": "7.0-test SMP",
                "version": version,
                "name": module.stem.replace("-", "_"),
            }[field]
            if wrong_kernel and field == "vermagic":
                value = "wrong-kernel"
            return subprocess.CompletedProcess(args, 0, value)
        if args[0] == "dpkg-deb" and "--build" in args:
            args[-1].write_bytes(b"deb")
        return subprocess.CompletedProcess(
            args, 0, "Package: linux-headers-7.0-test\nArchitecture: amd64\n"
        )

    monkeypatch.setattr(build.subprocess, "run", run)
    if wrong_kernel:
        with pytest.raises(BuildError, match="vermagic"):
            build.build(headers, source, tmp_path / "out", jobs=2, driver_version=version)
        assert output.read_bytes() == b"previous"
    else:
        assert (
            build.build(headers, source, tmp_path / "out", jobs=2, driver_version=version) == output
        )
        assert output.read_bytes() == b"deb"
    assert output.name == f"nvidia-modules-7.0-test_{version}-1_amd64.deb"
    make = next(command for command in commands if command[0] == "make")
    assert make[2] != source and "KERNEL_UNAME=7.0-test" in make
    assert any(str(arg).startswith("SYSSRC=") for arg in make)
    assert not list(source.rglob("*.ko"))
    assert not list(output.parent.glob("build-*"))


@pytest.mark.skipif(shutil.which("dpkg-deb") is None, reason="dpkg-deb required")
def test_real_deb_is_runtime_only_with_matching_kernel_dependencies(tmp_path: Path) -> None:
    source = source_tree(tmp_path / "source")
    modules = []
    for name in MODULES:
        path = source / "kernel-open" / f"{name}.ko"
        path.write_bytes(b"test-module")
        modules.append(path)
    output = tmp_path / "nvidia.deb"
    build.package(modules, source, "7.0-test", "615.78.08", "a" * 64, output)
    metadata = subprocess.check_output(["dpkg-deb", "-f", output], text=True)
    assert "ubuntu-uki-boot-installed (= 1.0-7.0-test)" in metadata
    assert "ubuntu-uki-boot-live (= 1.0-7.0-test)" in metadata
    assert "Depends: kmod," in metadata
    root = tmp_path / "extracted"
    subprocess.run(["dpkg-deb", "-R", output, root], check=True)
    assert len(list((root / "usr/lib/modules/7.0-test/updates/nvidia").glob("*.ko"))) == 4
    assert not (root / "usr/src").exists()
    assert not (root / "usr/lib/modules/7.0-test/build").exists()
    info = json.loads((root / "usr/share/doc/nvidia-modules-7.0-test/build.json").read_text())
    assert info["headers_sha256"] == "a" * 64
    for name in ("postinst", "postrm"):
        script = root / "DEBIAN" / name
        subprocess.run(["sh", "-n", script], check=True)
        assert "depmod -a 7.0-test" in script.read_text()


def test_invalid_jobs_leave_output_untouched(tmp_path: Path) -> None:
    with pytest.raises(BuildError, match="positive"):
        build.build(tmp_path / "headers", tmp_path / "source", tmp_path / "out", jobs=0)
    assert not (tmp_path / "out").exists()


def test_requested_driver_version_must_match_source_before_build(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = source_tree(tmp_path / "source")
    headers = tmp_path / "headers.deb"
    headers.touch()
    monkeypatch.setattr(build.shutil, "which", lambda tool: tool)
    run = Mock()
    monkeypatch.setattr(build.subprocess, "run", run)
    with pytest.raises(
        BuildError, match=r"requested NVIDIA 595\.80, but source contains 615\.78\.08"
    ):
        build.build(headers, source, tmp_path / "out", driver_version="595.80")
    run.assert_not_called()
    assert not (tmp_path / "out").exists()
