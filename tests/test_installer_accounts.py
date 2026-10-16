"""
Account credentials never reach command arguments or diagnostic output.
"""

import subprocess
from unittest.mock import Mock

import pytest

from ubuntu_uki_iso.errors import BuildError, NotConfirmed, Refusal
from ubuntu_uki_iso.installer import accounts
from ubuntu_uki_iso.installer.context import Context
from ubuntu_uki_iso.installer.preflight import RaidMode


@pytest.fixture(autouse=True)
def no_deployment_key(monkeypatch):
    monkeypatch.setattr(accounts.config, "SSH_PUBLIC_KEY", "")


def test_password_is_hidden_confirmed_and_sent_only_via_stdin(monkeypatch, tmp_path):
    ctx = Context(
        "/dev/test",
        [],
        RaidMode.NONE,
        dry_run=False,
        confirmed=True,
        payload=tmp_path / "rootfs",
    )
    commands = Mock(
        return_value=subprocess.CompletedProcess([], 0, stdout="root:x:0:0::/root:/bin/bash\n")
    )
    monkeypatch.setattr(subprocess, "run", commands)
    monkeypatch.setattr(
        accounts.getpass, "getpass", Mock(side_effect=["private-secret", "private-secret"])
    )
    credentials = accounts.collect(ctx, "alice", None)
    assert "private-secret" not in repr(credentials)
    accounts.create(ctx, credentials)
    assert credentials.password == ""
    calls = commands.call_args_list
    assert all("private-secret" not in repr(call.args) for call in calls)
    assert calls[-1].kwargs["input"] == "alice:private-secret\n"
    assert "--groups" in calls[-2].args[0] and "sudo" in calls[-2].args[0]


@pytest.mark.parametrize(
    "username,passwords",
    [("root", []), ("bad:name", []), ("alice", ["one", "two"]), ("alice", ["", ""])],
)
def test_invalid_credentials_refuse_before_creation(monkeypatch, tmp_path, username, passwords):
    ctx = Context("/dev/test", [], RaidMode.NONE, payload=tmp_path / "rootfs")
    command = Mock(
        return_value=subprocess.CompletedProcess([], 0, stdout="root:x:0:0::/root:/bin/bash\n")
    )
    monkeypatch.setattr(subprocess, "run", command)
    monkeypatch.setattr(accounts.getpass, "getpass", Mock(side_effect=passwords))
    with pytest.raises(Refusal):
        accounts.collect(ctx, username, None)
    assert all(call.args[0][0] == "unsquashfs" for call in command.call_args_list)


def test_private_password_file_required(monkeypatch, tmp_path):
    ctx = Context("/dev/test", [], RaidMode.NONE, payload=tmp_path / "rootfs")
    monkeypatch.setattr(
        subprocess, "run", Mock(return_value=subprocess.CompletedProcess([], 0, stdout=""))
    )
    path = tmp_path / "password"
    path.write_text("secret\n")
    path.chmod(0o644)
    with pytest.raises(Refusal, match="permissions"):
        accounts.collect(ctx, "alice", path)
    path.chmod(0o600)
    assert accounts.collect(ctx, "alice", path).password == "secret"


def test_creation_is_gated_and_failure_does_not_disclose_password(monkeypatch):
    ctx = Context("/dev/test", [], RaidMode.NONE)
    credentials = accounts.Credentials("alice", "secret")
    command = Mock(side_effect=subprocess.CalledProcessError(1, ["chpasswd"], stderr="secret"))
    monkeypatch.setattr(subprocess, "run", command)
    with pytest.raises(NotConfirmed):
        accounts.create(ctx, credentials)
    command.assert_not_called()
    ctx.dry_run = False
    ctx.confirmed = True
    with pytest.raises(BuildError) as error:
        accounts.create(ctx, credentials)
    assert "secret" not in str(error.value)
    assert credentials.password == ""


def test_ssh_key_is_added_once_with_private_permissions(monkeypatch, tmp_path):
    from ubuntu_uki_iso.installer import context

    monkeypatch.setattr(context, "TARGET_MOUNT", tmp_path)
    monkeypatch.setattr(accounts.config, "SSH_PUBLIC_KEY", "ssh-ed25519 TEST public-key")
    commands = Mock()
    monkeypatch.setattr(subprocess, "run", commands)
    ctx = Context("/dev/test", [], RaidMode.NONE, dry_run=False, confirmed=True)
    directory = tmp_path / "home/alice/.ssh"
    directory.mkdir(parents=True)
    authorized = directory / "authorized_keys"
    authorized.write_text("ssh-ed25519 EXISTING other-key")
    accounts.preload_key(ctx, "alice")
    accounts.preload_key(ctx, "alice")
    assert authorized.read_text() == "ssh-ed25519 EXISTING other-key\nssh-ed25519 TEST public-key\n"
    assert directory.stat().st_mode & 0o777 == 0o700
    assert authorized.stat().st_mode & 0o777 == 0o600
    assert commands.call_args.args[0] == [
        "chroot",
        str(tmp_path),
        "chown",
        "--reference=/home/alice",
        "/home/alice/.ssh",
        "/home/alice/.ssh/authorized_keys",
    ]


def test_invalid_configured_public_key_refuses_before_disk_changes(monkeypatch):
    monkeypatch.setattr(accounts.config, "SSH_PUBLIC_KEY", "invalid key")
    command = Mock(return_value=subprocess.CompletedProcess([], 1))
    monkeypatch.setattr(subprocess, "run", command)
    ctx = Context("/dev/test", [], RaidMode.NONE)
    with pytest.raises(Refusal, match="valid SSH public key"):
        accounts.collect(ctx, "alice", None)
    assert command.call_args.args[0] == ["ssh-keygen", "-l", "-f", "-"]


def test_ssh_key_preloading_requires_confirmation(monkeypatch, tmp_path):
    from ubuntu_uki_iso.installer import context

    monkeypatch.setattr(context, "TARGET_MOUNT", tmp_path)
    monkeypatch.setattr(accounts.config, "SSH_PUBLIC_KEY", "ssh-ed25519 TEST public-key")
    ctx = Context("/dev/test", [], RaidMode.NONE)
    with pytest.raises(NotConfirmed):
        accounts.preload_key(ctx, "alice")
    assert not (tmp_path / "home").exists()
