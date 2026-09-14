"""Output.

One rule matters here: the messages a person reads when something is refused
are part of the safety design, not decoration. A refusal that does not explain
itself teaches the operator to reach for a flag that turns the check off. So
:func:`refuse` exists and is never abbreviated.

Colour is emitted only when stdout is a terminal, and every helper is safe to
call when it is not — build logs get piped and captured constantly.
"""

from __future__ import annotations

import sys
from typing import TextIO

# The exit code a refusal uses, kept here so the console and the exception
# hierarchy cannot drift apart.
REFUSAL_EXIT = 2


class Console:
    """Console output with optional colour and an optional log file.

    A class rather than module-level functions so tests can capture output
    without monkeypatching the process's streams.
    """

    def __init__(
        self,
        *,
        stdout: TextIO | None = None,
        stderr: TextIO | None = None,
        colour: bool | None = None,
        log_path: object | None = None,
    ) -> None:
        self.out = stdout if stdout is not None else sys.stdout
        self.err = stderr if stderr is not None else sys.stderr
        self._colour = self.out.isatty() if colour is None else colour
        self._log = None
        if log_path is not None:
            # Best effort: the installer runs on machines where the log
            # directory may not be writable, and losing the log must never be
            # the reason an install fails.
            try:
                self._log = open(log_path, "a", encoding="utf-8")  # noqa: SIM115
            except OSError:
                self._log = None

    # -- colour ------------------------------------------------------------

    def _c(self, code: str, text: str) -> str:
        return f"\033[{code}m{text}\033[0m" if self._colour else text

    def bold(self, text: str) -> str:
        return self._c("1", text)

    def dim(self, text: str) -> str:
        return self._c("2", text)

    def red(self, text: str) -> str:
        return self._c("31", text)

    def green(self, text: str) -> str:
        return self._c("32", text)

    def yellow(self, text: str) -> str:
        return self._c("33", text)

    def blue(self, text: str) -> str:
        return self._c("34", text)

    # -- emission ----------------------------------------------------------

    def _write(self, text: str, *, err: bool = False) -> None:
        stream = self.err if err else self.out
        print(text, file=stream, flush=True)
        if self._log is not None:
            self._log.write(text + "\n")
            self._log.flush()

    # -- the vocabulary ----------------------------------------------------

    def step(self, message: str) -> None:
        """A section heading. `==> message`."""
        self._write(f"{self.blue('==>')} {self.bold(message)}")

    def info(self, message: str = "") -> None:
        self._write(f"    {message}" if message else "")

    def grey(self, message: str) -> None:
        """Detail that is true but rarely wanted."""
        self._write("    " + self.dim(message))

    def ok(self, message: str) -> None:
        self._write(f"    {self.green('ok')} {message}")

    def warn(self, message: str) -> None:
        self._write(f"{self.yellow('WARN:')} {message}", err=True)

    def error(self, message: str) -> None:
        self._write(f"{self.red('ERROR:')} {message}", err=True)

    def refuse(self, reason: str) -> None:
        """Print a refusal.

        Always to stderr, always in full. The reason is a paragraph on purpose
        -- see the module docstring.
        """
        self._write("")
        self._write(f"{self.red('REFUSING')}", err=True)
        self._write("")
        for line in reason.splitlines():
            self._write(line, err=True)

    def rule(self, title: str = "") -> None:
        self._write("")
        if title:
            self._write(f"{title}")
            self._write("=" * len(title))

    def close(self) -> None:
        if self._log is not None:
            self._log.close()
            self._log = None

    def __enter__(self) -> Console:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


# A module-level default. Library code takes a console argument; this exists so
# `ubuntu_uki_iso.cli` and small scripts have something to write to without
# threading one through by hand.
_console = Console()


def get_console() -> Console:
    return _console


def set_console(console: Console) -> None:
    global _console
    _console = console
