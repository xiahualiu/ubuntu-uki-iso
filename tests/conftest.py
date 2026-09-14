"""Shared test fixtures.

The fixtures here exist to make one thing easy: testing logic that would
otherwise need root, a container, or a real disk. Nothing in this suite touches
a block device, and the tests that exercise the installer's safety rules
construct the device state they need rather than discovering it.
"""

from __future__ import annotations

import io
from pathlib import Path
from typing import Any

import pytest

from ubuntu_uki_iso.log import Console
from ubuntu_uki_iso.proc import Result, Runner


class RecordingRunner(Runner):
    """A Runner that records instead of executing.

    ``probe`` is overridden too, so a test can assert that a read-only call was
    made without any command actually running. The real :class:`Runner` runs
    probes unconditionally, which is correct in production and unhelpful in a
    test.
    """

    def __init__(self, **kwargs: Any) -> None:
        kwargs.setdefault("dry_run", False)
        kwargs.setdefault(
            "console", Console(colour=False, stdout=io.StringIO(), stderr=io.StringIO())
        )
        super().__init__(**kwargs)
        self.probed: list[list[str]] = []
        self._responses: dict[str, Result] = {}

    def stub(self, match: str, stdout: str = "", returncode: int = 0) -> None:
        """Make any command whose joined argv contains ``match`` return this."""
        self._responses[match] = Result([match], returncode, stdout, "")

    # The overrides take the same parameters as :class:`Runner` so that a test
    # can call them exactly as it would the real thing. Everything that only
    # matters when a command actually runs — ``check``, ``timeout``, ``quiet``,
    # ``log``, ``cwd`` — is accepted and ignored.
    def probe(
        self,
        *argv: str | Path,
        check: bool = False,
        timeout: float | None = None,
        cwd: Path | None = None,
    ) -> Result:
        args = [str(a) for a in argv]
        self.probed.append(args)
        joined = " ".join(args)
        for match, result in self._responses.items():
            if match in joined:
                return Result(args, result.returncode, result.stdout, result.stderr)
        return Result(args, 0, "", "")

    def run(
        self,
        *argv: str | Path,
        check: bool = True,
        quiet: bool = False,
        log: Path | None = None,
        cwd: Path | None = None,
    ) -> Result:
        args = [str(a) for a in argv]
        self.executed.append(args)
        return Result(args, 0)

    def destructive(self, what: str, *argv: str | Path, check: bool = True) -> Result:
        args = [str(a) for a in argv]
        self.executed.append(args)
        return Result(args, 0)


@pytest.fixture
def runner() -> RecordingRunner:
    return RecordingRunner()


@pytest.fixture
def console() -> Console:
    return Console(colour=False, stdout=io.StringIO(), stderr=io.StringIO())


@pytest.fixture
def quiet_runner(console: Console) -> RecordingRunner:
    return RecordingRunner(console=console)
