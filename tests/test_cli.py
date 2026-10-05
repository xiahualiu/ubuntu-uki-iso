"""The CLI surface, and how a build step is launched.

The container is provided to the project, not managed by it, so the thing worth
testing is the contract that replaced the docker invocation: :func:`run_step`
runs a step as a child process in the checkout, with the environment the step's
own children — the mmdebstrap hooks — need. The commands that only existed to
drive docker are gone, and a regression that brought one back should fail here.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from unittest import mock

import pytest

from ubuntu_uki_iso.build import run_step
from ubuntu_uki_iso.cli import build_parser
from ubuntu_uki_iso.errors import CommandFailed


def _fake_run(seen: dict) -> object:
    def fake(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess:
        seen["argv"] = argv
        seen.update(kwargs)
        return subprocess.CompletedProcess(argv, seen.get("returncode", 0))

    return fake


def test_run_step_launches_the_module_in_the_checkout(tmp_path: Path) -> None:
    seen: dict = {}

    with mock.patch("ubuntu_uki_iso.build.subprocess.run", _fake_run(seen)):
        run_step("ubuntu_uki_iso.kernel.build", root=tmp_path)

    assert seen["argv"] == [
        sys.executable,
        "-m",
        "ubuntu_uki_iso.kernel.build",
    ]
    assert seen["cwd"] == tmp_path
    assert seen["env"]["UBUNTU_UKI_ISO_ROOT"] == str(tmp_path)
    # The step and the processes it spawns must import the checkout, not
    # anything installed beside it.
    assert seen["env"]["PYTHONPATH"].split(os.pathsep)[0] == str(tmp_path)


def test_run_step_keeps_the_callers_pythonpath(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PYTHONPATH", "/elsewhere")
    seen: dict = {}

    with mock.patch("ubuntu_uki_iso.build.subprocess.run", _fake_run(seen)):
        run_step("ubuntu_uki_iso.build.iso", root=tmp_path)

    assert seen["env"]["PYTHONPATH"] == f"{tmp_path}{os.pathsep}/elsewhere"


def test_run_step_inherits_environment_overrides(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Settings supplied by a parent process reach the build step."""
    monkeypatch.setenv("KERNEL_BUILD_TEST_SETTING", "enabled")
    seen: dict = {}

    with mock.patch("ubuntu_uki_iso.build.subprocess.run", _fake_run(seen)):
        run_step("ubuntu_uki_iso.kernel.build", root=tmp_path)

    assert seen["env"]["KERNEL_BUILD_TEST_SETTING"] == "enabled"


def test_run_step_raises_when_the_step_fails(tmp_path: Path) -> None:
    seen: dict = {"returncode": 1}

    with (
        mock.patch("ubuntu_uki_iso.build.subprocess.run", _fake_run(seen)),
        pytest.raises(CommandFailed),
    ):
        run_step("ubuntu_uki_iso.build.squashfs", root=tmp_path)


@pytest.mark.parametrize(
    "argv",
    [
        ["fix-perms"],
        ["build", "iso", "--rebuild-image"],
        ["doctor", "--rebuild-image"],
        ["clean", "--images"],
    ],
)
def test_commands_that_only_drove_docker_are_gone(argv: list[str]) -> None:
    with pytest.raises(SystemExit):
        build_parser().parse_args(argv)


def test_clean_still_parses() -> None:
    assert build_parser().parse_args(["clean"]).command == "clean"


@pytest.mark.parametrize(
    ("step", "module"),
    [
        ("kernel", "ubuntu_uki_iso.kernel.build"),
        ("uki", "ubuntu_uki_iso.uki.live"),
        ("uki-target", "ubuntu_uki_iso.uki.target"),
    ],
)
def test_build_routes_to_owning_subpackages(tmp_path: Path, step: str, module: str) -> None:
    from ubuntu_uki_iso.cli import cmd_build
    from ubuntu_uki_iso.log import Console
    from ubuntu_uki_iso.paths import Layout

    args = build_parser().parse_args(["build", step])
    with mock.patch("ubuntu_uki_iso.build.run_step") as launch:
        cmd_build(args, Layout(tmp_path), Console(colour=False))
    launch.assert_called_once_with(module, root=tmp_path)
