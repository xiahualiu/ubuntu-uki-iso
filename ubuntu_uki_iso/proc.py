"""Running external commands.

This module is where the installer's central promise is kept: **dry run by
default, and nothing mutates except through here.** If you find yourself
calling :mod:`subprocess` directly anywhere in this package, that is the bug —
a command that bypasses :class:`Runner` is a command that will run during a dry
run.

The distinction the API is built around:

:meth:`Runner.probe` / :meth:`Runner.capture`
    Read-only. Always executes, even in a dry run, because a plan built from
    imagined device state is worse than no plan. Anything that writes —
    including anything with an unpredictable side effect — does not belong
    here.

:meth:`Runner.run`
    Mutating. Printed and skipped in a dry run; executed otherwise.

:meth:`Runner.destructive`
    Mutating and unrecoverable. As :meth:`run`, plus a typed confirmation that
    no flag short of ``--assume-yes`` bypasses.
"""

from __future__ import annotations

import shlex
import shutil
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TypeVar

from .errors import CommandFailed, NotConfirmed, PackagesMissing, ToolMissing
from .log import Console, get_console

#: What a deferred value will turn out to be once the dry run is over. See
#: :meth:`Runner.value_or`.
_T = TypeVar("_T")


def _package_installed(name: str) -> bool:
    """Whether dpkg has this package installed.

    ``${db:Status-Abbrev}`` is two characters for a package in the desired
    state: ``ii`` installed, ``iU`` unpacked-but-unconfigured, and so on. Only
    ``ii`` counts — a half-configured package is one whose binaries may not be
    there yet.

    Anything unexpected — no dpkg, a timeout, a name dpkg has never heard of —
    answers "not installed", because that is the answer that produces a
    message rather than a later failure with no explanation.
    """
    try:
        result = subprocess.run(
            ["dpkg-query", "-W", "-f=${db:Status-Abbrev}", name],
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0 and result.stdout.startswith("ii")


@dataclass(frozen=True)
class Result:
    """The outcome of a command that was allowed to run."""

    argv: list[str]
    returncode: int
    stdout: str = ""
    stderr: str = ""

    @property
    def ok(self) -> bool:
        return self.returncode == 0

    def lines(self) -> list[str]:
        return [line for line in self.stdout.splitlines() if line.strip()]


@dataclass
class Runner:
    """Executes commands, or says what it would execute.

    ``dry_run`` defaults to True. That is not a convenience default; it is the
    property this project most needs to hold, and every caller that wants it
    off has to say so explicitly.
    """

    dry_run: bool = True
    confirmed: bool = False
    console: Console = field(default_factory=get_console)
    #: Every mutating command that was actually executed, in order. Tests read
    #: this to prove that a dry run ran nothing.
    executed: list[list[str]] = field(default_factory=list)
    #: Every command printed but not executed.
    skipped: list[list[str]] = field(default_factory=list)

    # -- prerequisites -----------------------------------------------------

    @staticmethod
    def require(*tools: str) -> None:
        """Fail with one message naming every missing tool.

        All of them at once rather than the first: discovering three missing
        packages one run at a time is a waste of a person's afternoon.
        """
        missing = [tool for tool in tools if shutil.which(tool) is None]
        if missing:
            raise ToolMissing(*missing)

    @staticmethod
    def require_packages(*packages: str) -> None:
        """Fail with one message naming every missing apt package.

        Read-only, and deliberately so: ``dpkg-query`` is asked what is
        installed and nothing is installed. This tool runs on machines it does
        not own — a CI runner, a container on one — and installing packages
        onto them would be both surprising and a change to an environment every
        other job shares. What it does instead is name the command, and leave
        running it to whoever owns the machine.

        Packages rather than commands, because that is the granularity the
        requirement is declared at (:func:`ubuntu_uki_iso.config.host_packages`)
        and because the fix is a package-level act.
        """
        missing = [name for name in packages if not _package_installed(name)]
        if missing:
            raise PackagesMissing(missing)

    @staticmethod
    def have(tool: str) -> bool:
        return shutil.which(tool) is not None

    # -- display -----------------------------------------------------------

    @staticmethod
    def fmt(argv: Sequence[str]) -> str:
        return shlex.join(str(a) for a in argv)

    # -- read-only ---------------------------------------------------------

    def probe(
        self,
        *argv: str | Path,
        check: bool = False,
        timeout: float | None = 120,
        cwd: Path | None = None,
    ) -> Result:
        """Run a read-only command and capture its output.

        Executes even in a dry run. ``check=False`` by default because the
        common case is asking a question the answer to which may legitimately
        be "no" — is there an md superblock here, is this mounted — and that is
        not an error.
        """
        args = [str(a) for a in argv]
        try:
            proc = subprocess.run(
                args,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
                cwd=cwd,
            )
        except FileNotFoundError:
            raise ToolMissing(args[0]) from None
        except subprocess.TimeoutExpired:
            return Result(args, returncode=124, stderr=f"timed out after {timeout}s")

        result = Result(args, proc.returncode, proc.stdout, proc.stderr)
        if check and not result.ok:
            raise CommandFailed(args, result.returncode, result.stderr)
        return result

    def capture(self, *argv: str | Path, check: bool = True, cwd: Path | None = None) -> str:
        """Read-only, returning stripped stdout. Empty string on failure unless
        ``check``."""
        result = self.probe(*argv, check=check, cwd=cwd)
        return result.stdout.strip()

    # -- mutating ----------------------------------------------------------

    def run(
        self,
        *argv: str | Path,
        check: bool = True,
        quiet: bool = False,
        log: Path | None = None,
        cwd: Path | None = None,
    ) -> Result:
        """Run a mutating command, or print it and skip it in a dry run.

        Output streams to the terminal by default — build steps take tens of
        minutes and a silent one is indistinguishable from a hung one.

        ``cwd`` matters more than it looks. ``make`` reads its configuration
        from the working directory, so a kernel build that forgets it
        configures one tree and builds another — or, more often, fails with
        "No targets specified and no makefile found" from wherever the process
        happened to be.
        """
        args = [str(a) for a in argv]

        if self.dry_run:
            self.skipped.append(args)
            self.console.info(f"{self.console.dim('DRY-RUN')}  {self.fmt(args)}")
            return Result(args, returncode=0)

        self.console.info(f"{self.console.green('+')} {self.fmt(args)}")
        self.executed.append(args)

        if log is not None:
            with open(log, "a", encoding="utf-8") as handle:
                proc = subprocess.run(
                    args,
                    stdout=handle,
                    stderr=subprocess.STDOUT,
                    check=False,
                    cwd=cwd,
                )
            result = Result(args, proc.returncode)
        else:
            stdout = subprocess.DEVNULL if quiet else None
            proc = subprocess.run(args, stdout=stdout, check=False, cwd=cwd)
            result = Result(args, proc.returncode)

        if check and not result.ok:
            raise CommandFailed(args, result.returncode)
        return result

    def destructive(self, what: str, *argv: str | Path, check: bool = True) -> Result:
        """Run a command that destroys data.

        Requires :attr:`confirmed` even outside a dry run. There is no flag
        that skips the confirmation other than ``--assume-yes``, which is
        itself explicit.
        """
        args = [str(a) for a in argv]

        if self.dry_run:
            self.skipped.append(args)
            self.console.info(f"{self.console.dim('DRY-RUN')}  {self.fmt(args)}")
            return Result(args, returncode=0)

        if not self.confirmed:
            raise NotConfirmed(
                f"refusing to run a destructive command without confirmation: {self.fmt(args)}"
            )

        self.console.info(f"{self.console.red('DESTROY')} {what}")
        self.console.info(f"{self.console.green('+')} {self.fmt(args)}")
        self.executed.append(args)

        proc = subprocess.run(args, check=False)
        result = Result(args, proc.returncode)
        if check and not result.ok:
            raise CommandFailed(args, result.returncode)
        return result

    # -- plans -------------------------------------------------------------

    def value_or(
        self, placeholder: str, func: Callable[..., _T], *args: object, **kwargs: object
    ) -> _T | str:
        """A value that only exists after a mutating step has run.

        In a dry run there is no such value, so the caller supplies the
        placeholder that belongs in the printed plan. This is what keeps one
        linear, auditable flow from having to be written twice — once to plan
        and once to execute.
        """
        if self.dry_run:
            return placeholder
        return func(*args, **kwargs)

    # -- confirmation ------------------------------------------------------

    def confirm(self, phrase: str, explain: str) -> None:
        """Ask for a typed phrase. No-op in a dry run.

        Reads from the controlling terminal rather than stdin so that a
        piped-in script cannot answer it by accident.
        """
        if self.dry_run:
            return

        self.console.warn(explain)
        try:
            with open("/dev/tty", encoding="utf-8") as tty:
                tty.write(f"Type {phrase} to proceed: ")
                tty.flush()
                answer = tty.readline().strip()
        except OSError as exc:
            raise NotConfirmed(
                "no terminal available for confirmation; "
                "pass --assume-yes only if this is automated"
            ) from exc

        if answer != phrase:
            raise NotConfirmed("confirmation phrase did not match — nothing was done")
        self.confirmed = True
