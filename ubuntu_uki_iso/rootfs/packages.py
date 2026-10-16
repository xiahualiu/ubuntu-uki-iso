"""
Package profiles used to bootstrap live and installed root filesystems.
"""

from collections.abc import Iterator
from dataclasses import dataclass


@dataclass(frozen=True)
class PackageList:
    """
    An immutable package profile.
    """

    variant: str
    entries: tuple[str, ...]

    def __len__(self) -> int:
        return len(self.entries)

    def __iter__(self) -> Iterator[str]:
        return iter(self.entries)

    def comma_joined(self) -> str:
        """
        The form mmdebstrap's ``--include`` wants.
        """
        return ",".join(self.entries)

    def as_args(self) -> list[str]:
        """
        The form apt wants.
        """
        return list(self.entries)
