"""
Collect login credentials before disk writes and create the installed sudo user.
"""

from __future__ import annotations

import getpass
import re
import subprocess
import warnings
from dataclasses import dataclass, field
from pathlib import Path

from ..errors import BuildError, Refusal
from . import config
from .context import Context


@dataclass
class Credentials:
    username: str
    password: str = field(repr=False)


def collect(ctx: Context, username: str | None, password_file: Path | None) -> Credentials:
    """
    Prompt on the terminal, hiding and confirming the password, before formatting.

    Automation may supply a username and a private password file instead.
    Reject accounts already present in the base image before any disk writes.
    """
    key = config.SSH_PUBLIC_KEY.strip()
    if key:
        result = subprocess.run(
            ["ssh-keygen", "-l", "-f", "-"],
            input=key + "\n",
            text=True,
            capture_output=True,
            check=False,
        )
        if "\n" in key or result.returncode != 0:
            raise Refusal("installer.config.SSH_PUBLIC_KEY must contain one valid SSH public key")
    if password_file is not None and not username:
        raise Refusal("--password-file requires --username")
    try:
        if not username:
            with open("/dev/tty", "r+", encoding="utf-8") as tty:
                tty.write("Username: ")
                tty.flush()
                username = tty.readline().strip()
        if not re.fullmatch(r"[a-z_][a-z0-9_-]{0,31}", username):
            raise Refusal(
                "username must be 1-32 lowercase letters, digits, underscores or hyphens, starting with a letter or underscore"
            )
        assert ctx.payload is not None
        passwd = subprocess.run(
            ["unsquashfs", "-cat", str(ctx.payload), "etc/passwd"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        if username == "root" or any(
            line.split(":", 1)[0] == username for line in passwd.splitlines()
        ):
            raise Refusal(f"account {username!r} already exists in the installed image")
        if password_file is not None:
            with password_file.open(encoding="utf-8") as stream:
                import os
                import stat

                info = os.fstat(stream.fileno())
                if (
                    not stat.S_ISREG(info.st_mode)
                    or info.st_mode & 0o077
                    or info.st_uid != os.geteuid()
                ):
                    raise Refusal(
                        "password file must be a regular file owned by the installer user with no group or other permissions"
                    )
                password = stream.read().removesuffix("\n")
        else:
            # Refuse getpass's fallback to visible input when there is no terminal.
            with warnings.catch_warnings():
                warnings.simplefilter("error", getpass.GetPassWarning)
                password = getpass.getpass("Initial password: ")
                if password != getpass.getpass("Confirm password: "):
                    raise Refusal("passwords do not match — nothing was changed")
        if not password or any(char in password for char in "\r\n\0"):
            raise Refusal("password must be nonempty and cannot contain newlines or NUL")
        return Credentials(username, password)
    except (OSError, EOFError, getpass.GetPassWarning) as exc:
        raise Refusal(
            "cannot read credentials; use a terminal or --username with --password-file"
        ) from exc


def create(ctx: Context, credentials: Credentials) -> None:
    """
    Create a sudo account; pass its password through stdin, never argv or logs.
    """
    ctx.require_apply()
    try:
        subprocess.run(
            [
                "chroot",
                str(ctx.target),
                "useradd",
                "--create-home",
                "--home-dir",
                f"/home/{credentials.username}",
                "--shell",
                "/bin/bash",
                "--groups",
                "sudo",
                credentials.username,
            ],
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["chroot", str(ctx.target), "chpasswd"],
            input=f"{credentials.username}:{credentials.password}\n",
            text=True,
            check=True,
            capture_output=True,
        )
        preload_key(ctx, credentials.username)
    except subprocess.CalledProcessError:
        raise BuildError("failed to create the installed login account") from None
    finally:
        credentials.password = ""
    print(f"Created login account {credentials.username} with sudo access")


def preload_key(ctx: Context, username: str) -> None:
    """
    Add the configured public key, preserving existing keys and securing permissions.
    """
    ctx.require_apply()
    key = config.SSH_PUBLIC_KEY.strip()
    if not key:
        return
    directory = ctx.target / "home" / username / ".ssh"
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    directory.chmod(0o700)
    authorized = directory / "authorized_keys"
    existing = authorized.read_text() if authorized.exists() else ""
    if key not in existing.splitlines():
        separator = "\n" if existing and not existing.endswith("\n") else ""
        authorized.write_text(existing + separator + key + "\n")
    authorized.chmod(0o600)
    subprocess.run(
        [
            "chroot",
            str(ctx.target),
            "chown",
            f"--reference=/home/{username}",
            f"/home/{username}/.ssh",
            f"/home/{username}/.ssh/authorized_keys",
        ],
        check=True,
        capture_output=True,
    )
    print(f"Preloaded SSH public key for {username}")
