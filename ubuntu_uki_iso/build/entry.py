"""Container entry point: run one build step, then hand the output back.

Equivalent to ``python -m <module>``, with one addition that matters.

Every build step runs as root inside the container, which means everything it
writes into the bind-mounted workspace is root-owned on the host. The host user
has no passwordless sudo, so a root-owned ``out/`` could not be deleted. The
``finally`` block below chowns it back on success, on failure, and on
``KeyboardInterrupt``.

If the container is killed hard enough to skip even that, ``ubuntu-uki-iso
fix-perms`` does the same job from a fresh container.

Usage::

    python3 -m ubuntu_uki_iso.build.entry ubuntu_uki_iso.build.kernel [args...]
"""

from __future__ import annotations

import contextlib
import importlib
import os
import sys
from pathlib import Path


def _chown_back() -> None:
    uid, gid = os.environ.get("HOST_UID"), os.environ.get("HOST_GID")
    if not uid or not gid:
        return

    out = Path("/work/out")
    if not out.is_dir():
        return

    # os.walk rather than shutil.chown recursion: this must not follow symlinks
    # out of the tree, and it must not raise on a file that vanished.
    #
    # Every failure here is swallowed on purpose. This runs in a finally block
    # on the way out of a build step, sometimes because that step already
    # failed; a chown that cannot complete must not replace the real error with
    # a permissions one. `ubuntu-uki-iso fix-perms` is the recovery path.
    with contextlib.suppress(OSError):
        for root, dirs, files in os.walk(out):
            for name in (*dirs, *files):
                os.chown(os.path.join(root, name), int(uid), int(gid), follow_symlinks=False)

    with contextlib.suppress(OSError):
        os.chown(out, int(uid), int(gid))


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if not argv:
        print("usage: python -m ubuntu_uki_iso.build.entry <module> [args...]", file=sys.stderr)
        return 2

    module_name, *rest = argv

    try:
        # Ensure the workspace copy of the package wins over anything
        # installed in the image, so the container always runs the code from
        # the checkout it is building.
        sys.path.insert(0, "/work")

        module = importlib.import_module(module_name)
        entry = getattr(module, "main", None)
        if entry is None:
            print(f"{module_name} has no main()", file=sys.stderr)
            return 2

        return int(entry(rest) or 0)
    finally:
        _chown_back()


if __name__ == "__main__":
    sys.exit(main())
