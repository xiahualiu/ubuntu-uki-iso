"""
Kernel input preparation without downloading or compiling a kernel.
"""

import subprocess
from pathlib import Path
from typing import Any

import pytest

from ubuntu_uki_iso.kernel import build
from ubuntu_uki_iso.kernel.folders import FolderLayout


def test_default_config_replaces_cached_ubuntu_and_custom_configs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    layout = FolderLayout(tmp_path)
    layout.require_dirs()
    source = tmp_path / "source"
    source.mkdir()
    (source / ".config").write_text("custom config")
    commands = []

    def run(argv: list[Any], **kwargs: Any) -> None:
        commands.append(argv)
        assert kwargs == {"cwd": source, "check": True}
        if argv == ["make", "ARCH=x86", "x86_64_defconfig"]:
            (source / ".config").write_text("CONFIG_X86_64=y\n")
        elif argv[0].endswith("merge_config.sh"):
            assert (source / ".config").read_text() == "CONFIG_X86_64=y\n"
            assert "CONFIG_LOCALVERSION=" in Path(argv[-1]).read_text()

    monkeypatch.setattr(build.subprocess, "run", run)
    assert build.configure(source) == source / ".config"
    assert commands == [
        ["make", "ARCH=x86", "x86_64_defconfig"],
        [
            str(source / "scripts/kconfig/merge_config.sh"),
            "-m",
            ".config",
            str(source / "config-trim.override"),
        ],
        ["make", "olddefconfig"],
        ["make", "syncconfig"],
    ]


@pytest.mark.parametrize("nested", [False, True])
def test_source_tarball_can_be_direct_or_nested(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, nested: bool
) -> None:
    layout = FolderLayout(tmp_path)
    name = "usr/src/" + ("linux-source-7.0.0/" if nested else "")
    name += "linux-source-7.0.0.tar.bz2"
    deb = tmp_path / "source.deb"
    monkeypatch.setattr(build, "download_package", lambda *args: deb)

    def extract(argv: list[Any], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        assert argv == ["dpkg-deb", "--extract", str(deb), str(layout.download_dir / "package")]
        assert kwargs == {"check": True}
        tarball = layout.download_dir / "package" / name
        tarball.parent.mkdir(parents=True)
        tarball.write_bytes(b"source")
        if nested:
            # Ubuntu also ships a top-level symlink to the nested tarball.
            (tarball.parent.parent / tarball.name).symlink_to(tarball)
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(build.subprocess, "run", extract)

    def unpack(tarball: Path, destination: Path) -> Path:
        assert tarball.read_bytes() == b"source"
        assert destination == layout.download_dir
        return destination / "linux-source-7.0.0"

    monkeypatch.setattr(build, "unpack_source", unpack)
    assert build.fetch_source(layout.download_dir) == layout.download_dir / "linux-source-7.0.0"


@pytest.mark.parametrize("missing", [None, "headers"])
def test_collects_bindeb_packages_and_excludes_debug_symbols(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, missing: bool
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    output = tmp_path / "out"
    names = {
        "image": "linux-image-7.0.14-xiahualab-iso_1_amd64.deb",
        "headers": "linux-headers-7.0.14-xiahualab-iso_1_amd64.deb",
    }
    for package_type, name in names.items():
        if package_type != missing:
            (tmp_path / name).write_bytes(name.encode())
    (tmp_path / "linux-image-7.0.0-generic-dbg_1_amd64.deb").touch()
    (tmp_path / "linux-libc-dev_1_amd64.deb").touch()
    (source / "debian").mkdir()
    (source / "debian/files").write_text(
        "".join(f"{name} kernel optional\n" for kind, name in names.items() if kind != missing)
        + "linux-image-7.0.0-generic-dbg_1_amd64.deb kernel optional\n"
        + "linux-libc-dev_1_amd64.deb devel optional\n"
    )
    # A previous release in the output directory must not affect collection.
    (tmp_path / "linux-image-6.0-old_1_amd64.deb").touch()
    monkeypatch.setattr(build.subprocess, "run", lambda *args, **kwargs: None)
    if missing is not None:
        with pytest.raises(build.BuildError, match="linux-headers"):
            build.build_packages(source, 2, output, "7.0.14-xiahualab-iso")
        assert not list(output.glob("*.deb"))
    else:
        build.build_packages(source, 2, output, "7.0.14-xiahualab-iso")
        assert {p.name for p in output.glob("*.deb")} == set(names.values())
        for name in names.values():
            assert (output / name).read_bytes() == name.encode()
