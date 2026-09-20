"""Exceptions.

The exception type is the control flow in this codebase, so the hierarchy is
worth reading before the rest.

The distinction that matters is :class:`Refusal` versus :class:`BuildError`.
A refusal is not a bug — it is the safety design working. It means the
installer looked at the machine, did not like what it saw, and stopped without
changing anything. Something that treats the two the same (a bare ``except``,
a nonzero exit with no message) throws away the most useful thing this project
produces.
"""

from __future__ import annotations


class XiahualabError(Exception):
    """Base class. Anything raised deliberately derives from this."""


class ConfigError(XiahualabError):
    """The configuration is internally inconsistent or refers to something
    that does not exist.

    Raised for a trim fragment naming a symbol the base config has never heard
    of, a package list that does not resolve, a cmdline missing its root=
    placeholder. These are caught before any expensive work starts.
    """


class BuildError(XiahualabError):
    """A build step failed."""


class ToolMissing(XiahualabError):
    """A required command is not on PATH."""

    def __init__(self, *tools: str) -> None:
        self.tools = tools
        super().__init__("missing required command(s): " + ", ".join(tools))


class PackagesMissing(XiahualabError):
    """A required package is not installed.

    Distinct from :class:`ToolMissing`, which is about one command: a package
    can be declared as a requirement without its binary being on this PATH, and
    the fix is a package-level act.

    The message names the command that fixes it and does not run it. This tool
    is used on machines it does not own — CI runners, containers — and changing
    them would change every other job that shares them.
    """

    def __init__(self, packages: list[str]) -> None:
        self.packages = tuple(packages)
        super().__init__(
            "missing required package(s): "
            + ", ".join(self.packages)
            + "\n\nInstall them with:\n    apt-get install -y "
            + " ".join(self.packages)
            + "\n\nThe list is declared in the package's data/packages/host.list, and the\n"
            "tool only ever checks it — see the Requirements section of the README."
        )


class CommandFailed(XiahualabError):
    """An external command exited nonzero and the caller asked for that to be
    fatal."""

    def __init__(self, argv: list[str], returncode: int, stderr: str = "") -> None:
        self.argv = argv
        self.returncode = returncode
        self.stderr = stderr
        detail = f": {stderr.strip()}" if stderr.strip() else ""
        super().__init__(f"command failed ({returncode}): {' '.join(argv)}{detail}")


class Refusal(XiahualabError):
    """The installer declined to act.

    Exits 2. Distinct from every other failure on purpose: a refusal means the
    machine was examined and left exactly as it was found, which is a
    successful outcome for the thing this project is most afraid of.
    """

    exit_code = 2

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


class NotConfirmed(XiahualabError):
    """A destructive step was reached without the typed confirmation."""

    exit_code = 3
