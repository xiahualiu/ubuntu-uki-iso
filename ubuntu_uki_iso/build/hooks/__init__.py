"""mmdebstrap customize hooks, one per rootfs image.

There are two rootfs images rather than one stripped at install time. Stripping
during install would make the destructive step also the complicated one, and
``packages/{live,installed}.list`` only means something if both are built. The
installer stays a copy operation.

These modules are invoked by mmdebstrap, not imported by the CLI::

    mmdebstrap --customize-hook="python3 -m ubuntu_uki_iso.build.hooks.live $1" ...

so each one's ``main()`` takes the target directory as its first argument.
"""

from __future__ import annotations

__all__ = ["common", "installed", "live"]
