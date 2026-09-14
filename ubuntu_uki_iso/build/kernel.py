"""Build the custom kernel.

Ubuntu's ``linux-source-7.0.0`` package, trimmed by a config fragment, built
with ``make bindeb-pkg``.

Two things about this step are worth knowing before reading it.

**The release string is never assumed.** ``linux-source-7.0.0`` carries
upstream 7.0.14 — the package name is the kernel *series*, not the version. So
the release is read back from the source tree with ``make kernelrelease`` and
recorded in ``out/kernel/release``, which becomes the only authority every
downstream step consults. Guessing "7.0.0-ubuntu-uki-iso" would have produced a
build that failed twenty minutes later, in the ISO, looking for a file named
after a kernel that does not exist.

**The guard check runs before the compile, not after.** A trim that breaks
Docker still builds. Finding out costs forty-five minutes and a boot cycle;
checking first costs a second.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tarfile
import time
from dataclasses import dataclass
from pathlib import Path

from .. import settings
from ..config import kernel as kconfig
from ..errors import BuildError
from ..log import Console, get_console
from ..paths import Layout
from ..proc import Runner

#: Scratch space, generous because a kernel build with debug info uses ~15 GB.
REQUIRED_SPACE_GB = 30

#: Wall-clock expectation, for the message that tells someone this is the long
#: one. Measured at -j32 with debug info on.
EXPECTED_MINUTES_DEBUG = 45
EXPECTED_MINUTES_FAST = 15


@dataclass
class KernelBuild:
    layout: Layout
    runner: Runner
    console: Console
    debug_info: bool = True

    # -- cheap paths -------------------------------------------------------

    @property
    def base_config_path(self) -> Path:
        return self.layout.kernel / "base.config"

    @property
    def source_dir(self) -> Path | None:
        """An already-extracted source tree, if there is one.

        Reusing it turns the second and later runs from a 190 MB download plus
        an unpack into nothing at all.
        """
        parent = self.layout.work / "src"
        if not parent.is_dir():
            return None
        for path in sorted(parent.iterdir()):
            if path.is_dir() and path.name.startswith("linux-source-"):
                return path
        return None

    # -- apt ---------------------------------------------------------------

    def _ensure_apt_lists(self) -> None:
        """Run ``apt-get update`` once per invocation if it has not run.

        The build image drops ``/var/lib/apt/lists`` to stay small, so package
        metadata has to be fetched before the first download. Until then the
        directory holds only ``lock`` and ``partial/``.
        """
        lists = Path("/var/lib/apt/lists")
        if lists.is_dir():
            meaningful = [
                entry for entry in lists.iterdir() if entry.name not in ("lock", "partial")
            ]
            if meaningful:
                return

        self.console.step("apt-get update")
        self.runner.run("apt-get", "update", "-qq", check=True)

    def _download(self, package: str, dest: Path) -> Path:
        """Download a .deb with apt, reporting apt's own error on failure.

        Runs from inside ``dest`` because ``apt-get download`` writes to the
        working directory, and captures stderr so that "Unable to locate
        package" reaches the person reading the failure instead of being
        swallowed into a generic message.
        """
        self._ensure_apt_lists()
        dest.mkdir(parents=True, exist_ok=True)

        proc = subprocess.run(
            ["apt-get", "download", package],
            cwd=dest,
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode != 0:
            raise BuildError(
                f"could not download {package}:\n"
                f"{proc.stderr.strip() or proc.stdout.strip() or '(no output)'}"
            )

        debs = sorted(dest.glob(f"{package}_*.deb"))
        if not debs:
            raise BuildError(f"apt reported success but {package} produced no .deb in {dest}")
        return debs[-1]

    # -- base config -------------------------------------------------------

    def fetch_base_config(self) -> Path:
        """The untrimmed Ubuntu config we start from.

        Taken from the same apt suite as the source, so the config base matches
        the source exactly rather than being a copy of whatever happens to be
        in ``/boot`` on the build host. ``/boot/config-<ver>`` ships inside
        ``linux-modules-<ver>``, not the image package.
        """
        if self.base_config_path.is_file() and self.base_config_path.stat().st_size:
            self.console.grey("base config already fetched")
            return self.base_config_path

        self.console.step(f"fetch base config ({settings.BASE_KERNEL})")
        package = f"linux-modules-{settings.BASE_KERNEL}"
        scratch = self.layout.work / "baseconfig"
        if scratch.exists():
            shutil.rmtree(scratch)
        scratch.mkdir(parents=True, exist_ok=True)

        deb = self._download(package, scratch)

        member = f"./boot/config-{settings.BASE_KERNEL}"
        listing = subprocess.run(
            ["dpkg-deb", "--fsys-tarfile", str(deb)],
            capture_output=True,
            check=False,
        )
        if listing.returncode != 0:
            raise BuildError(f"could not read {deb.name}")

        extract = subprocess.run(
            ["tar", "-x", "-C", str(scratch), member],
            input=listing.stdout,
            capture_output=True,
            check=False,
        )
        if extract.returncode != 0:
            raise BuildError(
                f"could not extract {member} from {deb.name}:\n"
                f"{extract.stderr.decode(errors='replace').strip()}"
            )

        extracted = scratch / member.lstrip("./")
        if not extracted.is_file():
            raise BuildError(f"{member} was not in {deb.name}")

        shutil.copyfile(extracted, self.base_config_path)
        lines = len(self.base_config_path.read_text(errors="replace").splitlines())
        self.console.info(f"base config: {self.base_config_path.name} ({lines} lines)")
        return self.base_config_path

    # -- source ------------------------------------------------------------

    def fetch_source(self) -> Path:
        existing = self.source_dir
        if existing is not None:
            self.console.grey("kernel source already extracted")
            return existing

        self.console.step(f"fetch source ({settings.KERNEL_SOURCE_PKG})")
        scratch = self.layout.work / "src"
        scratch.mkdir(parents=True, exist_ok=True)

        deb = self._download(settings.KERNEL_SOURCE_PKG, scratch)
        self.console.info(f"extracting {deb.name} ...")

        listing = subprocess.run(
            ["dpkg-deb", "--fsys-tarfile", str(deb)], capture_output=True, check=False
        )
        if listing.returncode != 0:
            raise BuildError(f"could not read {deb.name}")

        subprocess.run(
            ["tar", "-x", "-C", str(scratch), "--wildcards", "./usr/src/linux-source-*.tar.*"],
            input=listing.stdout,
            capture_output=True,
            check=False,
        )

        tarballs = sorted((scratch / "usr/src").glob("linux-source-*.tar.*"))
        if not tarballs:
            raise BuildError(f"no source tarball inside {deb.name}")

        tarball = tarballs[0]
        self.console.info(f"unpacking {tarball.name} (this takes a minute) ...")
        self._unpack(tarball, scratch)

        found = self.source_dir
        if found is None:
            raise BuildError("the kernel source did not unpack where expected")
        return found

    @staticmethod
    def _unpack(tarball: Path, dest: Path) -> None:
        mode = "r:bz2" if tarball.suffix == ".bz2" else "r:*"
        with tarfile.open(tarball, mode) as archive:
            archive.extractall(dest, filter="data")


def _check_space(path: Path, needed_gb: int, console: Console) -> None:
    usage = shutil.disk_usage(path if path.exists() else path.parent)
    available_gb = usage.free // (1024**3)
    if available_gb < needed_gb:
        raise BuildError(
            f"need about {needed_gb} GB free under {path}, have {available_gb} GB.\n"
            "A kernel build with debug info is not small."
        )
    console.grey(f"disk: {available_gb} GB available")


def _fragment_paths(debug_info: bool) -> list[Path]:
    fragments = [kconfig.trim_fragment()]
    if not debug_info:
        fragments.append(kconfig.nodebug_fragment())
    return fragments


def _configure(build: KernelBuild, source: Path, base_config: Path) -> Path:
    """Merge the trim into the base config and resolve dependencies."""
    console, runner = build.console, build.runner

    console.step("apply trim")
    config = source / ".config"
    shutil.copyfile(base_config, config)

    fragments = _fragment_paths(build.debug_info)
    if build.debug_info:
        console.info("debug info: ON (base config default; BTF stays enabled)")
    else:
        console.info("debug info: OFF (fast build)")

    # -m = merge only, do not run make. Dependencies are resolved explicitly
    # with olddefconfig afterwards so the sequence is the same every time.
    #
    # cwd is the source tree for both: merge_config.sh resolves ".config"
    # relative to where it runs, and `make` reads its configuration from the
    # working directory. Without it they run in the build container's /work,
    # where there is no kernel to configure.
    runner.run(
        str(source / "scripts/kconfig/merge_config.sh"),
        "-m",
        ".config",
        *[str(f) for f in fragments],
        check=True,
        cwd=source,
    )
    runner.run("make", "olddefconfig", check=True, cwd=source)

    # `olddefconfig` writes .config and stops there. `include/config/auto.conf`
    # — which is what `scripts/setlocalversion` reads to build the release
    # string — is regenerated by a *later* make invocation, and `kernelrelease`
    # is a phony target with no prerequisites, so it does not trigger one.
    #
    # The consequence, found by changing CONFIG_LOCALVERSION on an
    # already-configured tree: `.config` says one thing, `make kernelrelease`
    # reports the *previous* value, and everything downstream — the recorded
    # release, the UKI filename, /lib/modules/<rel> — is named after a kernel
    # that was never built. A first build on a fresh tree is fine, which is
    # what makes this unpleasant to find.
    runner.run("make", "syncconfig", check=True, cwd=source)

    # Record what the trim actually did, for auditing.
    merged = config.read_text(errors="replace")
    base = base_config.read_text(errors="replace")

    changes = _symbol_changes(base, merged)
    diff_path = build.layout.kernel / "config.diff"
    diff_path.write_text(
        "".join(f"-{symbol}={before}\n+{symbol}={after}\n" for symbol, before, after in changes),
        encoding="utf-8",
    )
    console.info(f"{len(changes)} config symbols differ from base")
    return config


def _symbol_changes(base: str, merged: str) -> list[tuple[str, str, str]]:
    """Per-symbol differences between two kernel configs.

    A symbol-level diff rather than a textual one, for two reasons.

    The practical one: a sorted textual diff of a .config is unreadable, and
    its output cannot be counted reliably — the counts this project quotes are
    of *decisions*, not of lines that moved.

    The conceptual one: the fragment is the intent and the merged config is the
    outcome, and they differ wherever Kconfig ``select`` pulls something back
    in. What is worth knowing is which symbols ended up different, and what
    they were before.
    """
    base_values = kconfig.parse_config_values(base)
    merged_values = kconfig.parse_config_values(merged)

    changes = []
    for symbol in sorted(set(base_values) | set(merged_values)):
        before = base_values.get(symbol, "<absent>")
        after = merged_values.get(symbol, "<absent>")
        if before != after:
            changes.append((symbol, _show(before), _show(after)))
    return changes


def _show(value: str | None) -> str:
    return "n" if value is None else value


def _verify_guard(build: KernelBuild, config: Path, base_config: Path) -> None:
    """The check that protects Docker, run before the compile."""
    console = build.console
    console.step("verify the never-disable guard")

    merged_text = config.read_text(errors="replace")
    base_text = base_config.read_text(errors="replace")

    # A guard entry naming a symbol the base config has never heard of cannot
    # guard anything. That is almost always a typo, and a typo here silently
    # weakens the protection, so it is fatal rather than a warning.
    known = kconfig.parse_config_values(base_text)
    entries = kconfig.parse_guard()
    unknown = [entry for entry in entries if entry.symbol not in known]
    if unknown:
        listing = "\n".join(
            f"  {entry.symbol} (never-disable.list:{entry.lineno})" for entry in unknown
        )
        raise BuildError(
            "these never-disable entries are not symbols in the base config:\n"
            f"{listing}\n"
            "A guard entry that does not exist cannot protect anything. Check the "
            "spelling against data/kernel/never-disable.list."
        )

    failures = kconfig.check_guard(merged_text, entries)
    if failures:
        listing = "\n".join(f"  {failure}" for failure in failures)
        raise BuildError(
            "the never-disable guard FAILED — the trim removes something this "
            f"project needs:\n{listing}\n\n"
            "Fix data/kernel/config-trim.fragment. If the symbol is only missing "
            "because a menu gate above it was disabled, note that disabling a "
            "menuconfig symbol removes everything in its block."
        )

    console.info(f"{len(entries)} symbols checked, all present")


def _report_unknown_fragment_symbols(build: KernelBuild, base_config: Path) -> None:
    """Mention trim lines that did nothing.

    Not fatal — a symbol can legitimately disappear between kernel versions —
    but the usual cause is a typo, and a typo'd trim line is a trim that
    silently did not happen.
    """
    unknown = kconfig.unknown_fragment_symbols(
        base_config.read_text(errors="replace"), kconfig.trim_fragment()
    )
    if unknown:
        for symbol in sorted(unknown):
            build.console.warn(
                f"trim fragment mentions {symbol}, which is not in the base config — it did nothing"
            )
    else:
        build.console.grey("every trim symbol exists in the base config")


def _release(build: KernelBuild, source: Path) -> str:
    """The kernel release, straight from the source tree.

    Checked before the expensive build so that a version surprise costs
    seconds. The ``-ubuntu-uki-iso`` suffix comes from ``CONFIG_LOCALVERSION`` in
    the trim fragment; if it is missing here it will be missing in the UKI
    filename, the module directory, and every downstream path.
    """
    # Runs in the source tree; `make` reads the .config from its working
    # directory, so there is no version of this that works from elsewhere.
    proc = subprocess.run(
        ["make", "-s", "kernelrelease"],
        cwd=source,
        capture_output=True,
        text=True,
        check=False,
    )
    release = proc.stdout.strip()

    if not release:
        raise BuildError("make kernelrelease returned nothing")
    if not release.endswith(settings.LOCALVERSION):
        raise BuildError(
            f"the kernel release is {release!r}, which does not end in "
            f"{settings.LOCALVERSION!r}.\n"
            "Check CONFIG_LOCALVERSION in data/kernel/config-trim.fragment."
        )
    return release


def _build_packages(build: KernelBuild, source: Path) -> tuple[Path, Path]:
    console = build.console
    jobs = os.cpu_count() or 1

    console.step(f"build (make bindeb-pkg -j{jobs})")
    if build.debug_info:
        console.info(f"this is the long one — roughly {EXPECTED_MINUTES_DEBUG} minutes at -j32")
    else:
        console.info(f"debug info is off — roughly {EXPECTED_MINUTES_FAST} minutes at -j32")
    console.info(f"watching: {build.layout.kernel / 'build.log'}")

    started = time.monotonic()
    build.runner.run(
        "make",
        "bindeb-pkg",
        f"-j{jobs}",
        check=True,
        log=build.layout.kernel / "build.log",
        cwd=source,
    )
    console.info(f"built in {int(time.monotonic() - started) // 60} min")

    # bindeb-pkg drops the .debs in the parent of the source tree.
    parent = source.parent
    images = [p for p in parent.glob("linux-image-*.deb") if "-dbg_" not in p.name]
    headers = list(parent.glob("linux-headers-*.deb"))

    if not images:
        raise BuildError("the build produced no linux-image .deb")
    if not headers:
        raise BuildError("the build produced no linux-headers .deb")

    image, header = sorted(images)[-1], sorted(headers)[-1]
    for deb in (image, header):
        shutil.copyfile(deb, build.layout.kernel / deb.name)
        console.info(f"packaged: {deb.name}")

    # The -dbg package is deliberately not shipped: hundreds of megabytes that
    # nothing here consumes.
    for dbg in parent.glob("linux-image-*-dbg_*.deb"):
        console.grey(f"skipping {dbg.name} (not shipped)")
    return image, header


# ---------------------------------------------------------------------------
# entry points
# ---------------------------------------------------------------------------


def check_only(layout: Layout | None = None, console: Console | None = None) -> str:
    """Validate the trim against the real base config and report the release.

    Builds nothing. This is the answer to "getting these wrong wastes forty-five
    minutes": it downloads the base config and the source, merges, resolves,
    checks the guard, and prints what the release will be.
    """
    layout = layout or Layout()
    console = console or get_console()
    layout.require_dirs()

    runner = Runner(dry_run=False, console=console)
    build = KernelBuild(layout, runner, console)

    base_config = build.fetch_base_config()
    _report_unknown_fragment_symbols(build, base_config)

    console.step("check-only")
    runner.require("make", "tar", "dpkg-deb")

    source = build.fetch_source()
    config = _configure(build, source, base_config)
    _verify_guard(build, config, base_config)

    release = _release(build, source)
    console.info(f"source tree version : {release.removesuffix(settings.LOCALVERSION)}")
    console.info(f"kernel release      : {release}")

    layout.kernel_release_file.write_text(release + "\n", encoding="utf-8")
    console.info("check-only passed; out/kernel/release written")
    return release


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    console = get_console()
    layout = Layout()

    if "--check-only" in argv:
        check_only(layout, console)
        return 0

    layout.require_dirs()
    runner = Runner(dry_run=False, console=console)
    build = KernelBuild(layout, runner, console)

    base_config = build.fetch_base_config()
    _report_unknown_fragment_symbols(build, base_config)

    _check_space(layout.out, REQUIRED_SPACE_GB, console)

    source = build.fetch_source()
    config = _configure(build, source, base_config)
    _verify_guard(build, config, base_config)

    release = _release(build, source)
    layout.kernel_release_file.write_text(release + "\n", encoding="utf-8")
    console.info(f"kernel release: {release}")

    _build_packages(build, source)

    console.step("kernel: done")
    console.info(f"release: {release}")
    for deb in sorted(layout.kernel.glob("*.deb")):
        console.info(f"  {deb.name}  {deb.stat().st_size // (1024 * 1024)} MB")
    return 0


if __name__ == "__main__":
    sys.exit(main())
