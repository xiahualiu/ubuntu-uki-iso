"""
Standalone UKI CLI input selection and EFI output paths.
"""

from pathlib import Path
from unittest.mock import Mock

import pytest

from ubuntu_uki_iso.errors import BuildError
from ubuntu_uki_iso.settings import Mode
from ubuntu_uki_iso.uki import build as uki_build
from ubuntu_uki_iso.uki.folders import FolderLayout


def test_selects_recorded_kernel_and_rejects_ambiguity(tmp_path: Path) -> None:
    layout = FolderLayout(tmp_path)
    layout.kernel.mkdir()
    current = layout.kernel / "linux-image-7.0-test_1_amd64.deb"
    current.touch()
    (layout.kernel / "linux-image-6.0-old_1_amd64.deb").touch()
    (layout.kernel / "linux-image-7.0-test-dbg_1_amd64.deb").touch()
    with pytest.raises(BuildError, match="found 2"):
        layout.kernel_image_deb()
    (layout.kernel / "release").write_text("7.0-test\n")
    assert layout.kernel_image_deb() == current


def test_preflight_failure_does_not_create_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        uki_build,
        "preflight",
        lambda *args: (_ for _ in ()).throw(BuildError("missing packages: dracut")),
    )
    root = tmp_path / "new"
    assert uki_build.main(["--temp-dir", str(root)]) == 1
    assert not root.exists()


@pytest.mark.parametrize(
    ("extra", "filename", "mode"),
    [([], "uki.efi", Mode.INSTALLED), (["--mode", Mode.LIVE], "BOOTX64.EFI", Mode.LIVE)],
)
def test_cli_builds_efi_from_kernel_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, extra: list[str], filename: str, mode: Mode
) -> None:
    layout = FolderLayout(tmp_path)
    layout.kernel.mkdir()
    image = layout.kernel / "linux-image-7.0-test_1_amd64.deb"
    image.touch()
    monkeypatch.setattr(uki_build, "preflight", Mock())
    build = Mock()
    monkeypatch.setattr(uki_build, "build", build)
    assert uki_build.main(["--temp-dir", str(tmp_path), *extra]) == 0
    assert build.call_args.args[:2] == (image, layout.uki / filename)
    assert build.call_args.kwargs == {"mode": mode}
    assert not list(layout.uki.glob("*.deb"))


def test_cli_explicit_input(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    image = tmp_path / "custom.deb"
    output = tmp_path / "uki/uki.efi"
    monkeypatch.setattr(uki_build, "preflight", Mock())
    build = Mock()
    monkeypatch.setattr(uki_build, "build", build)
    assert (
        uki_build.main(
            [
                "--temp-dir",
                str(tmp_path),
                "--kernel-deb",
                str(image),
            ]
        )
        == 0
    )
    assert build.call_args.args[:2] == (image, output)
    assert build.call_args.kwargs == {"mode": Mode.INSTALLED}


def test_preflight_reports_missing_packages(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import subprocess

    monkeypatch.setattr(
        uki_build.subprocess,
        "run",
        Mock(return_value=subprocess.CompletedProcess([], 0, "Listing...\n", "")),
    )
    with pytest.raises(BuildError, match="install with: apt-get install -y"):
        uki_build.preflight(tmp_path / "new")
    assert not (tmp_path / "new").exists()


def test_main_reports_failed_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import subprocess

    monkeypatch.setattr(uki_build, "preflight", Mock())
    monkeypatch.setattr(
        uki_build,
        "build",
        Mock(side_effect=subprocess.CalledProcessError(1, ["dracut", "--force"])),
    )
    assert (
        uki_build.main(
            [
                "--temp-dir",
                str(tmp_path),
                "--kernel-deb",
                str(tmp_path / "kernel.deb"),
            ]
        )
        == 1
    )
    assert "command failed (1): dracut --force" in capsys.readouterr().err


def test_cli_rejects_cmdline_override(tmp_path: Path) -> None:
    with pytest.raises(SystemExit):
        uki_build.main(["--temp-dir", str(tmp_path), "--cmdline", "root=test"])


def test_command_line_uses_config_definition(monkeypatch: pytest.MonkeyPatch) -> None:
    from ubuntu_uki_iso.uki import config

    monkeypatch.setattr(
        config,
        "installed_command_line",
        lambda: config.KernelCommandLine(("root=UUID=test", "ro", "debug")),
    )
    assert config.cmdline(Mode.INSTALLED) == "root=UUID=test ro debug"


def test_cli_rejects_output_override(tmp_path: Path) -> None:
    with pytest.raises(SystemExit):
        uki_build.main(["--temp-dir", str(tmp_path), "--output", str(tmp_path / "custom.efi")])


def test_preflight_requires_storage_userspace() -> None:
    from ubuntu_uki_iso.uki import config

    assert {"mdadm", "dmsetup"} <= set(config.required_packages())
