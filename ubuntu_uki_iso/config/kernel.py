"""The kernel trim, and the guard that keeps it honest.

The failure this module exists to prevent has a specific and unpleasant shape:
a ``# CONFIG_X is not set`` line disables something the system needs. The
kernel still builds. The box still boots. The array still mounts. Docker
starts. And then, days later and very far from the cause, ``docker run`` fails
with something about runc and cgroups that says nothing about a kernel config.

The same shape catches anything else the project depends on, and the guard list
is not only about Docker. The one that actually bit was ``SCSI_VIRTIO``: it
lives inside the ``if SCSI_LOWLEVEL`` menu block, so a single line disabling
that gate removed it without naming it — while a comment a few lines away
claimed it was being kept. Nothing was guarding it, because at the time this
list was framed as "Docker-critical". The test suite's only symptom was a
machine with no disks.

So the guard list is checked mechanically, in both directions:

* every symbol on it must survive the merge (:func:`check_guard`)
* every symbol the trim mentions must actually exist in the base config
  (:func:`unknown_fragment_symbols`) — a typo'd trim line is a trim that
  silently did not happen

Both run before the compile. A trim that breaks something still builds, so
finding out afterwards costs forty-five minutes and a boot cycle; finding out
first costs a second.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from ..paths import data_file

TRIM_FRAGMENT = "kernel/config-trim.fragment"
NODEBUG_FRAGMENT = "kernel/config-nodebug.fragment"
NEVER_DISABLE = "kernel/never-disable.list"
INSTALL_CONF = "kernel/install.conf"

#: A symbol entry in a list file: comments stripped, blank lines dropped.
_ENTRY_RE = re.compile(r"^\s*([A-Z0-9_]+)\s*(?:=\s*(\S+))?\s*$")

#: `CONFIG_FOO=y` or `# CONFIG_FOO is not set`
_CONFIG_SET_RE = re.compile(r"^(CONFIG_[A-Za-z0-9_]+)=(.*)$")
_CONFIG_UNSET_RE = re.compile(r"^# (CONFIG_[A-Za-z0-9_]+) is not set$")


def list_entries(path: Path) -> list[str]:
    """Comment-stripped, non-empty lines.

    Shared by the package lists and the guard list so the files stay readable
    without every consumer reimplementing the parsing.
    """
    entries = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if line:
            entries.append(line)
    return entries


def trim_fragment() -> Path:
    return data_file(TRIM_FRAGMENT)


def nodebug_fragment() -> Path:
    return data_file(NODEBUG_FRAGMENT)


def never_disable() -> Path:
    return data_file(NEVER_DISABLE)


def install_conf() -> Path:
    return data_file(INSTALL_CONF)


# ---------------------------------------------------------------------------
# The guard list
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class GuardEntry:
    """One line of ``never-disable.list``.

    ``CONFIG_FOO`` requires y or m, ``CONFIG_FOO=y`` requires exactly y,
    ``CONFIG_FOO=m`` accepts y or m — a built-in satisfies a module
    requirement, but not the reverse.
    """

    symbol: str
    requirement: str | None = None  # None, "y", or "m"
    lineno: int = 0

    def satisfied_by(self, value: str | None) -> bool:
        if value is None:
            return False
        if self.requirement == "y":
            return value == "y"
        if self.requirement == "m":
            return value in ("y", "m")
        return value in ("y", "m")

    def describe(self) -> str:
        return f"{self.symbol}={self.requirement}" if self.requirement else self.symbol


def parse_guard(path: Path | None = None) -> list[GuardEntry]:
    """Parse the never-disable list.

    A malformed line is an error rather than a skip: a guard entry that
    silently does not parse is a guard entry that silently does not guard.
    """
    from ..errors import ConfigError

    path = path or never_disable()
    entries: list[GuardEntry] = []

    for lineno, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        match = _ENTRY_RE.match(line)
        if not match:
            raise ConfigError(
                f"{path}:{lineno}: cannot parse guard entry: {raw.strip()!r}\n"
                "Expected CONFIG_SYMBOL, CONFIG_SYMBOL=y or CONFIG_SYMBOL=m."
            )
        symbol, value = match.group(1), match.group(2)
        if not symbol.startswith("CONFIG_"):
            raise ConfigError(
                f"{path}:{lineno}: guard entries must be kernel symbols, got {symbol!r}"
            )
        if value is not None and value not in ("y", "m"):
            raise ConfigError(f"{path}:{lineno}: requirement must be 'y' or 'm', got {value!r}")
        entries.append(GuardEntry(symbol, value, lineno))

    if not entries:
        raise ConfigError(f"{path} contains no guard entries — that cannot be right")
    return entries


def parse_config_values(text: str) -> dict[str, str | None]:
    """Kernel ``.config`` text to ``{symbol: value}``.

    Unset symbols map to ``None`` so the difference between "disabled" and
    "not mentioned" stays visible.
    """
    values: dict[str, str | None] = {}
    for line in text.splitlines():
        if match := _CONFIG_SET_RE.match(line):
            values[match.group(1)] = match.group(2).strip().strip('"')
        elif match := _CONFIG_UNSET_RE.match(line):
            values[match.group(1)] = None
    return values


def fragment_symbols(fragment: Path) -> set[str]:
    """Every symbol a fragment touches, whether it sets or unsets it."""
    symbols = set()
    for raw in fragment.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if (match := _CONFIG_SET_RE.match(line)) or (match := _CONFIG_UNSET_RE.match(line)):
            symbols.add(match.group(1))
    return symbols


def disabled_symbols(fragment: Path) -> set[str]:
    """Only the symbols a fragment turns *off*.

    This is the side that matters for the guard: a fragment that enables
    something cannot break Docker.
    """
    return {
        match.group(1)
        for raw in fragment.read_text(encoding="utf-8").splitlines()
        if (match := _CONFIG_UNSET_RE.match(raw.strip()))
    }


def check_guard(
    merged_config_text: str,
    entries: list[GuardEntry] | None = None,
) -> list[str]:
    """Return one message per guard entry the merged config violates.

    Empty list means the kernel can run containers. Anything else means the
    build must stop before it spends forty-five minutes compiling.
    """
    entries = entries if entries is not None else parse_guard()
    values = parse_config_values(merged_config_text)

    failures = []
    for entry in entries:
        value = values.get(entry.symbol)
        if not entry.satisfied_by(value):
            shown = "not set" if value is None else f"={value}"
            failures.append(
                f"{entry.symbol} is {shown} but the never-disable list requires "
                f"{entry.describe()} (never-disable.list:{entry.lineno})"
            )
    return failures


def unknown_fragment_symbols(base_config_text: str, fragment: Path) -> set[str]:
    """Symbols the fragment mentions that the base config has never heard of.

    Not fatal by itself — a symbol can legitimately disappear between kernel
    versions — but almost always a typo, and a typo'd trim line is a trim that
    silently did not happen.
    """
    known = parse_config_values(base_config_text)
    return {symbol for symbol in fragment_symbols(fragment) if symbol not in known}


def guard_overlap(
    guard: list[GuardEntry] | None = None,
    fragment: Path | None = None,
) -> list[str]:
    """Guarded symbols that the trim fragment disables.

    This is the cheap half of ``check-config``: it needs no kernel source and
    no network, so it can run in a second and in CI on every push.
    """
    guard = guard if guard is not None else parse_guard()
    fragment = fragment or trim_fragment()
    disabled = disabled_symbols(fragment)
    return [entry.symbol for entry in guard if entry.symbol in disabled]
