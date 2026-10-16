"""
Direct subprocess execution keeps installer planning and confirmation separate from writes.
"""

import os
import subprocess
from collections.abc import Callable
from pathlib import Path
from unittest.mock import Mock, mock_open

import pytest

from ubuntu_uki_iso.errors import NotConfirmed
from ubuntu_uki_iso.installer import cli, partition, raid, target, uki
from ubuntu_uki_iso.installer.context import Context
from ubuntu_uki_iso.installer.preflight import Decision, Preflight, RaidMode


@pytest.mark.parametrize("dry_run", [True, False])
@pytest.mark.parametrize("step", [partition.run, target.run, uki.run, raid._create])
def test_mutating_steps_require_apply_and_confirmation(
    monkeypatch: pytest.MonkeyPatch, dry_run: bool, step: Callable[[Context], None]
) -> None:
    commands = Mock(side_effect=AssertionError("unconfirmed step executed a command"))
    monkeypatch.setattr(subprocess, "run", commands)
    ctx = Context("/dev/test", [], RaidMode.NONE, dry_run=dry_run)
    if dry_run:
        step(ctx)
    else:
        with pytest.raises(NotConfirmed):
            step(ctx)
    commands.assert_not_called()


@pytest.mark.parametrize("mode", ["dry-run", "assume-yes", "wrong-confirmation"])
def test_cli_only_dispatches_writes_after_confirmation(
    monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    commands = Mock(side_effect=AssertionError("CLI unexpectedly executed an external command"))
    monkeypatch.setattr(subprocess, "run", commands)
    monkeypatch.setattr(cli.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    is_dir = Path.is_dir
    monkeypatch.setattr(
        Path, "is_dir", lambda path: str(path) == "/sys/firmware/efi" or is_dir(path)
    )
    monkeypatch.setattr(cli.target, "find_payload", Mock())
    monkeypatch.setattr(cli.preflight, "scan", lambda disks: Preflight([]))
    monkeypatch.setattr(cli.preflight, "report", Mock())
    monkeypatch.setattr(cli.preflight, "check_root_disk", Mock())
    monkeypatch.setattr(cli, "_cleanup", Mock())
    monkeypatch.setattr(
        cli.accounts, "collect", Mock(return_value=cli.accounts.Credentials("testuser", "secret"))
    )
    monkeypatch.setattr(cli.accounts, "create", Mock())
    dispatched = []

    def step(ctx: Context, *args: object) -> None:
        ctx.require_apply()
        dispatched.append(ctx)

    for module in (cli.partition, cli.raid, cli.target, cli.uki, cli.efientry):
        monkeypatch.setattr(module, "run", step)
    argv = ["--root-disk", "/dev/test", "--raid", "none"]
    if mode != "dry-run":
        argv.append("--apply")
    if mode == "assume-yes":
        argv.append("--assume-yes")
    args = cli.build_parser().parse_args(argv)
    if mode == "wrong-confirmation":
        monkeypatch.setattr("builtins.open", mock_open(read_data="wrong\n"))
        with pytest.raises(NotConfirmed, match="did not match"):
            cli.install(args)
    else:
        assert cli.install(args) == 0
    assert len(dispatched) == (5 if mode == "assume-yes" else 0)
    commands.assert_not_called()


def test_command_failure_is_reported_without_traceback(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        cli, "install", Mock(side_effect=subprocess.CalledProcessError(1, ["sgdisk"]))
    )
    assert cli.main([]) == 1


def test_raid_dispatch_does_not_write_in_dry_run(monkeypatch: pytest.MonkeyPatch) -> None:
    commands = Mock(side_effect=AssertionError("dry-run RAID dispatch executed a command"))
    monkeypatch.setattr(subprocess, "run", commands)
    ctx = Context("/dev/test", [], RaidMode.CREATE)
    raid.run(ctx, Preflight([], decision=Decision.CREATE))
    commands.assert_not_called()


def test_default_auto_mode_prints_detected_disks_and_create_plan_before_confirmation(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    from ubuntu_uki_iso.installer.preflight import DiskScan

    disks = [f"/dev/sd{name}" for name in "abcdefgh"]
    monkeypatch.setattr(cli.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(cli.discovery, "select", lambda *args, **kwargs: ("/dev/nvme0n1", disks))
    monkeypatch.setattr(cli.target, "find_payload", Mock())
    monkeypatch.setattr(
        cli.preflight,
        "scan",
        lambda devices: Preflight([DiskScan(path, True, False, None, ()) for path in devices]),
    )
    monkeypatch.setattr(cli.preflight, "report", Mock())
    monkeypatch.setattr(cli.preflight, "check_root_disk", Mock())
    commands = Mock(side_effect=AssertionError("dry-run must not format any disks"))
    monkeypatch.setattr(subprocess, "run", commands)
    assert cli.install(cli.build_parser().parse_args([])) == 0
    output = capsys.readouterr().out
    assert "CREATE a new raid0 array across 8 disks" in output
    assert "root: partition /dev/nvme0n1" in output
    assert all(path in output for path in disks)
    commands.assert_not_called()
