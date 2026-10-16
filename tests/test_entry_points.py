"""
Installed commands resolve directly to their owning submodules.
"""

import importlib
import tomllib
from pathlib import Path

import pytest

from ubuntu_uki_iso.qemu import harness

SCRIPTS = tomllib.loads((Path(__file__).parents[1] / "pyproject.toml").read_text())["project"][
    "scripts"
]


@pytest.mark.parametrize("entry", SCRIPTS.values())
def test_entry_point_help(entry, capsys):
    module, name = entry.split(":")
    main = getattr(importlib.import_module(module), name)
    with pytest.raises(SystemExit) as result:
        main(["--help"])
    assert result.value.code == 0
    assert "usage:" in capsys.readouterr().out


def test_qemu_entry_point_dispatches_selected_test(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(
        harness, "run_tests", lambda name, layout: calls.append((name, layout.root)) or 0
    )
    assert harness.main(["--temp-dir", str(tmp_path), "--test", "live-boot"]) == 0
    assert calls == [("live-boot", tmp_path)]


@pytest.mark.parametrize("module_name", ["uki.build", "rootfs.build"])
def test_invalid_image_mode_is_rejected_before_build(module_name, tmp_path, capsys):
    module = importlib.import_module(f"ubuntu_uki_iso.{module_name}")
    argv = ["--temp-dir", str(tmp_path), "--mode", "invalid"]
    if module_name == "rootfs.build":
        argv += ["--boot-deb", str(tmp_path / "boot.deb")]
    with pytest.raises(SystemExit) as result:
        module.main(argv)
    assert result.value.code == 2
    assert "invalid Mode value" in capsys.readouterr().err
    assert not list(tmp_path.iterdir())
