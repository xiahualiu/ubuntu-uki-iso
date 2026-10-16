"""
Test-side model for the kernel guard-entry contract.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from ubuntu_uki_iso.errors import ConfigError


@dataclass(frozen=True)
class GuardEntry:
    """
    A required kernel symbol used to test guard requirement semantics.
    """

    symbol: str
    requirement: str | None = None

    def __post_init__(self) -> None:
        if not re.fullmatch(r"CONFIG_[A-Za-z0-9_]+", self.symbol):
            raise ConfigError(f"invalid guarded symbol: {self.symbol}")
        if self.requirement not in (None, "y", "m"):
            raise ConfigError(f"invalid guard requirement: {self.requirement}")

    def satisfied_by(self, value: str | None) -> bool:
        if value is None:
            return False
        if self.requirement == "y":
            return value == "y"
        return value in ("y", "m")
