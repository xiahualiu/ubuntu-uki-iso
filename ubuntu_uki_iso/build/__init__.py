"""The build pipeline.

Each step is a module with a ``main(argv)`` so it can be run standalone inside
the container::

    python3 -m ubuntu_uki_iso.build.kernel
    python3 -m ubuntu_uki_iso.build.rootfs live
    python3 -m ubuntu_uki_iso.build.uki
    python3 -m ubuntu_uki_iso.build.squashfs
    python3 -m ubuntu_uki_iso.build.iso

The CLI wraps them, and runs every one of them inside the build container
rather than on the host — see :mod:`ubuntu_uki_iso.build.container` for why that is
a constraint rather than a preference.
"""

from __future__ import annotations

# No `__all__`: the steps below are run as `python3 -m ...`, never imported
# here, so naming them in `__all__` would promise names this module never binds.
