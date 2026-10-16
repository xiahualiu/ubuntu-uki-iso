"""
Installed commands resolve directly to their owning submodules.
"""

import importlib
import tomllib
from pathlib import Path

import pytest

from ubuntu_uki_iso.qemu import harness
from ubuntu_uki_iso.qemu.folders import FolderLayout

SCRIPTS = tomllib.loads((Path(__file__).parents[1] / "pyproject.toml").read_text())["project"][
    "scripts"
]


@pytest.mark.parametrize("entry", SCRIPTS.values())
def test_entry_point_help(entry: str, capsys: pytest.CaptureFixture[str]) -> None:
    module, name = entry.split(":")
    main = getattr(importlib.import_module(module), name)
    with pytest.raises(SystemExit) as result:
        main(["--help"])
    assert result.value.code == 0
    assert "usage:" in capsys.readouterr().out


def test_qemu_entry_point_dispatches_selected_test(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = []

    def run_tests(name: str, layout: FolderLayout) -> int:
        calls.append((name, layout.root))
        return 0

    monkeypatch.setattr(harness, "run_tests", run_tests)
    assert harness.main(["--temp-dir", str(tmp_path), "--test", "live-boot"]) == 0
    assert calls == [("live-boot", tmp_path)]


@pytest.mark.parametrize("module_name", ["uki.build", "rootfs.build"])
def test_invalid_image_mode_is_rejected_before_build(
    module_name: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    module = importlib.import_module(f"ubuntu_uki_iso.{module_name}")
    argv = ["--temp-dir", str(tmp_path), "--mode", "invalid"]
    if module_name == "rootfs.build":
        argv += ["--boot-deb", str(tmp_path / "boot.deb")]
    with pytest.raises(SystemExit) as result:
        module.main(argv)
    assert result.value.code == 2
    assert "invalid Mode value" in capsys.readouterr().err
    assert not list(tmp_path.iterdir())
