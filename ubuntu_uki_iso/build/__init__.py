"""The build pipeline.

Each step is a module with a ``main(argv)`` so it can be run standalone::

    python3 -m ubuntu_uki_iso.kernel.build --temp-dir out
    python3 -m ubuntu_uki_iso.rootfs.build --temp-dir out --mode live
    python3 -m ubuntu_uki_iso.uki.build --temp-dir out --mode live
    python3 -m ubuntu_uki_iso.iso.build --temp-dir out

:func:`run_step` is how the CLI launches them, each as a child process in the
environment the CLI itself is running in. That environment is expected to be a
container — the one built from ``build/Dockerfile`` — but the container is
provided to the project, not managed by it; nothing here invokes docker.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from ..errors import CommandFailed

# No `__all__`: the steps below are run as `python3 -m ...`, never imported
# here, so naming them in `__all__` would promise names this module never binds.


def run_step(module: str, *args: str, root: Path) -> None:
    """Run one build step as ``python3 -m <module>``, in the checkout.

    A child process rather than an import, because that is the contract every
    step module keeps (``def main(argv)`` plus a ``__main__`` guard), because a
    forty-five-minute kernel build should stream its output and report failure
    as an exit code, and because the steps themselves shell out to mmdebstrap,
    make and xorriso, which are happier with their own process to themselves.

    The environment is what a container used to supply:

    * ``cwd`` and ``PYTHONPATH`` are the checkout root, so the steps and the
      processes *they* spawn — notably the mmdebstrap ``--customize-hook`` —
      import the code being built rather than anything installed alongside it.
    * ``UBUNTU_UKI_ISO_ROOT`` pins the layout for those grandchildren, which
      resolve it by walking up from whatever directory they inherit.
    * everything else is inherited for the external build tools.
    """
    env = dict(os.environ)
    existing = env.get("PYTHONPATH")
    env["PYTHONPATH"] = os.pathsep.join(p for p in (str(root), existing) if p)
    env["UBUNTU_UKI_ISO_ROOT"] = str(root)

    argv = [sys.executable, "-m", module, *args]
    result = subprocess.run(argv, cwd=root, env=env, check=False)
    if result.returncode != 0:
        raise CommandFailed(argv, result.returncode)
