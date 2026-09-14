"""Running build steps in the build container.

Why every build step runs in a container: the build host has no passwordless
sudo, so ``mmdebstrap``, ``chroot``, ``mount`` and ``kernel-install`` have
nowhere else to run. That is a hard constraint of this machine, not a
preference.

What is deliberately *not* here, and why it is worth stating explicitly:

* ``--privileged``
* any mount of ``/dev`` or the host's block devices

The container gets ``SYS_ADMIN`` and an unconfined seccomp profile, because
those are what mounting and chrooting need, and nothing else. It mounts the
workspace and nothing else. So it cannot see ``/dev/sd{a..h}``, and a bug in a
build script has no path to the 58 TB array. The protection is structural
rather than procedural.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from .. import settings
from ..errors import BuildError, CommandFailed, ToolMissing
from ..log import Console, get_console
from ..paths import Layout
from ..proc import Runner

#: Environment variables forwarded into the container, so that overriding a
#: setting on the host overrides it in the build too. Without this, a
#: `UBUNTU_UKI_ISO_VOLID=OTHER ubuntu-uki-iso build iso` would silently build the
#: default label.
_ENV_PREFIX = settings.ENV_PREFIX


@dataclass
class ContainerRunner:
    """Builds images and runs steps inside them."""

    layout: Layout
    console: Console = field(default_factory=get_console)

    @property
    def docker(self) -> str:
        return settings.DOCKER

    def _docker_argv(self, *args: str) -> list[str]:
        return [self.docker, *args]

    def _require_docker(self) -> None:
        if shutil.which(self.docker) is None:
            raise ToolMissing(self.docker)

    # -- images ------------------------------------------------------------

    def build_image(self, target: str = "build", tag: str | None = None) -> None:
        """Build the build (or test) container image."""
        self._require_docker()
        tag = tag or (settings.TEST_IMAGE if target == "test" else settings.BUILD_IMAGE)

        self.console.step(f"building container image: {tag}")
        dockerfile = self.layout.root / "build" / "Dockerfile"
        if not dockerfile.is_file():
            raise BuildError(
                f"no Dockerfile at {dockerfile}\n"
                "The container definitions still live in build/Dockerfile."
            )

        argv = self._docker_argv(
            "build",
            "--target",
            target,
            "-f",
            str(dockerfile),
            "-t",
            tag,
            str(self.layout.root),
        )
        self.console.info(Runner.fmt(argv))
        result = subprocess.run(argv, check=False)
        if result.returncode != 0:
            raise CommandFailed(argv, result.returncode)

    def image_exists(self, tag: str) -> bool:
        if shutil.which(self.docker) is None:
            return False
        probe = subprocess.run(
            self._docker_argv("image", "inspect", tag),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        return probe.returncode == 0

    def ensure_image(self, target: str = "build") -> None:
        tag = settings.TEST_IMAGE if target == "test" else settings.BUILD_IMAGE
        if not self.image_exists(tag):
            self.build_image(target)

    # -- running steps -----------------------------------------------------

    def docker_argv(
        self, module: str, *args: str, image: str | None = None, mounts: tuple[str, ...] = ()
    ) -> list[str]:
        """The full ``docker run`` for a build step."""
        argv = [
            "run",
            "--rm",
            "--cap-add",
            "SYS_ADMIN",
            "--security-opt",
            "seccomp=unconfined",
            "--security-opt",
            "apparmor=unconfined",
            "-e",
            f"HOST_UID={os.getuid()}",
            "-e",
            f"HOST_GID={os.getgid()}",
            "-e",
            "PYTHONPATH=/work",
        ]
        for key, value in sorted(os.environ.items()):
            if key.startswith(_ENV_PREFIX):
                argv += ["-e", f"{key}={value}"]

        for mount in mounts:
            argv += ["-v", mount]

        argv += [
            "-v",
            f"{self.layout.root}:/work",
            "-w",
            "/work",
            image or settings.BUILD_IMAGE,
            "python3",
            "-m",
            "ubuntu_uki_iso.build.entry",
            module,
            *args,
        ]
        return self._docker_argv(*argv)

    def run_step(
        self, module: str, *args: str, image: str | None = None, mounts: tuple[str, ...] = ()
    ) -> None:
        """Run one build step, streaming its output."""
        self._require_docker()
        argv = self.docker_argv(module, *args, image=image, mounts=mounts)
        result = subprocess.run(argv, check=False)
        if result.returncode != 0:
            raise CommandFailed(argv, result.returncode)

    # -- housekeeping ------------------------------------------------------

    def fix_perms(self) -> None:
        """Hand ``out/`` back to the invoking user.

        Build steps run as root inside the container, so everything they write
        into the bind-mounted workspace comes back root-owned. The host user
        has no passwordless sudo, so a root-owned ``out/`` would be impossible
        to delete. The container entry point chowns on the way out; this is the
        recovery path for a build that was killed hard enough to skip that.
        """
        self._require_docker()
        if not self.layout.out.exists():
            return

        argv = self._docker_argv(
            "run",
            "--rm",
            "-v",
            f"{self.layout.root}:/work",
            "ubuntu:26.04",
            "chown",
            "-R",
            f"{os.getuid()}:{os.getgid()}",
            "/work/out",
        )
        result = subprocess.run(argv, check=False)
        if result.returncode != 0:
            raise CommandFailed(argv, result.returncode)
        self.console.info(f"out/ now owned by {os.getuid()}:{os.getgid()}")

    def remove_tree(self, path: Path) -> None:
        """Delete a tree, from a container if needed.

        A root-owned file cannot be removed by the host user, and ``rm -rf``
        gives up partway with a permission error while leaving the rest. Doing
        it in a container sidesteps the ownership question entirely.
        """
        if not path.exists():
            return

        try:
            shutil.rmtree(path)
            return
        except PermissionError:
            pass

        self._require_docker()
        argv = self._docker_argv(
            "run",
            "--rm",
            "-v",
            f"{self.layout.root}:/work",
            "ubuntu:26.04",
            "rm",
            "-rf",
            f"/work/{path.relative_to(self.layout.root)}",
        )
        result = subprocess.run(argv, check=False)
        if result.returncode != 0:
            raise CommandFailed(argv, result.returncode)
