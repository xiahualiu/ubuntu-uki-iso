"""The dry-run guarantee.

The single most important property in this codebase: a dry run runs nothing
that mutates. These tests assert it directly rather than trusting that every
caller remembered.
"""

from __future__ import annotations

import pytest

from ubuntu_uki_iso.errors import NotConfirmed
from ubuntu_uki_iso.proc import Runner


def test_dry_run_skips_mutating_commands(console):
    runner = Runner(dry_run=True, console=console)

    runner.run("mkfs.ext4", "/dev/sda1")

    assert runner.executed == []
    assert runner.skipped == [["mkfs.ext4", "/dev/sda1"]]


def test_dry_run_skips_destructive_commands(console):
    runner = Runner(dry_run=True, console=console)

    runner.destructive("erase the disk", "sgdisk", "--zap-all", "/dev/sda")

    assert runner.executed == []
    assert runner.skipped == [["sgdisk", "--zap-all", "/dev/sda"]]


def test_dry_run_still_runs_read_only_probes(console):
    """A plan built from imagined device state is worse than no plan."""
    runner = Runner(dry_run=True, console=console)

    result = runner.probe("true")

    assert result.ok
    assert result.returncode == 0


def test_probe_returns_a_result_for_a_command_that_says_no(console):
    """`mdadm --examine` on a device with no superblock exits nonzero.

    That is the ordinary "no" answer, not a failure, so probe does not raise.
    """
    runner = Runner(dry_run=True, console=console)
    result = runner.probe("false")
    assert not result.ok
    assert result.returncode == 1


def test_probe_raises_when_the_command_does_not_exist(console):
    """A missing binary is not the same as a command answering "no".

    Silently returning empty output here would look exactly like "no md
    superblock on this disk", which is the wrong conclusion to reach because
    mdadm is not installed.
    """
    from ubuntu_uki_iso.errors import ToolMissing

    runner = Runner(dry_run=True, console=console)
    with pytest.raises(ToolMissing):
        runner.probe("/nonexistent/command/xyz")


def test_destructive_refuses_without_confirmation(console):
    """Not merely skipped — an error, so a forgotten confirm cannot pass silently."""
    runner = Runner(dry_run=False, confirmed=False, console=console)

    with pytest.raises(NotConfirmed):
        runner.destructive("erase the disk", "true")

    assert runner.executed == []


def test_destructive_runs_once_confirmed(console):
    runner = Runner(dry_run=False, confirmed=True, console=console)

    runner.destructive("erase the disk", "true")

    assert runner.executed == [["true"]]


def test_confirm_is_a_no_op_in_a_dry_run(console):
    runner = Runner(dry_run=True, console=console)
    runner.confirm("destroy /dev/sda", "this would delete everything")
    assert runner.confirmed is False


def test_value_or_returns_the_placeholder_in_a_dry_run(console):
    """What lets one linear flow serve as both plan and execution."""
    runner = Runner(dry_run=True, console=console)
    assert runner.value_or("<uuid>", lambda: "real") == "<uuid>"

    live = Runner(dry_run=False, console=console)
    assert live.value_or("<uuid>", lambda: "real") == "real"


def test_require_names_every_missing_tool_at_once(console):
    """Discovering three missing packages one run at a time wastes an afternoon."""
    from ubuntu_uki_iso.errors import ToolMissing

    with pytest.raises(ToolMissing) as caught:
        Runner.require("this-does-not-exist", "neither-does-this")

    assert len(caught.value.tools) == 2


def test_fmt_quotes_arguments_that_need_it(console):
    assert Runner.fmt(["echo", "a b"]) == "echo 'a b'"
