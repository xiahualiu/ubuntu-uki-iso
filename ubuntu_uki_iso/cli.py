"""
The ubuntu-uki-iso command.

This replaces what was a Makefile. The targets were always thin wrappers around
shell scripts, and the interesting part — which flag reaches which step, and
what a failure means — was spread across both. Here it is in one place, with
argparse doing the parsing and the exception hierarchy doing the exit codes.

Two ways in, and both are the guard at the bottom of this file: the
``ubuntu-uki-iso`` console script declared in ``[project.scripts]``, and
``python -m ubuntu_uki_iso.cli``. There is deliberately no ``__main__.py`` —
one module holding the entry point beats two that each hold half of it.
"""

from __future__ import annotations

import argparse
import os
import sys

from . import __version__, settings
from .errors import Refusal, XiahualabError
from .log import Console, set_console
from .paths import Layout

# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ubuntu-uki-iso",
        description=(
            "Build and install a purpose-built Ubuntu image: a custom kernel,\n"
            "delivered as a UKI the UEFI firmware boots directly."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "typical use\n"
            "  ubuntu-uki-iso doctor            validate the kernel trim before building it\n"
            "  ubuntu-uki-iso build kernel      ~45 minutes with debug info\n"
            "  ubuntu-uki-iso build iso         rootfs, UKI, squashfs, xorriso\n"
            "  ubuntu-uki-iso test vm           QEMU, against virtual disks only\n"
        ),
    )
    parser.add_argument("--version", action="version", version=f"ubuntu-uki-iso {__version__}")
    parser.add_argument("--no-colour", action="store_true", help="disable ANSI colour")
    parser.add_argument("--log", metavar="PATH", help="also append output to PATH")

    subparsers = parser.add_subparsers(dest="command", metavar="<command>")

    _add_build(subparsers)
    _add_doctor(subparsers)
    _add_verify(subparsers)
    _add_install(subparsers)
    _add_test(subparsers)
    _add_housekeeping(subparsers)

    # Not internal, despite being called by a systemd unit rather than a
    # person: `ubuntu-uki-iso install --bless` refuses to act without the file it
    # writes, so it is worth being able to find it.
    subparsers.add_parser(
        "mark-booted",
        help="record that this kernel booted (called by xiahualab-mark-booted.service)",
    )

    return parser


def _add_build(subparsers: argparse._SubParsersAction) -> None:
    build = subparsers.add_parser(
        "build",
        help="build the kernel, rootfs images, UKI, squashfs or ISO",
        description="Every step runs inside the build container.",
    )
    build.add_argument(
        "step",
        choices=["kernel", "rootfs", "uki", "squashfs", "iso", "all"],
        help="which step to run",
    )
    build.add_argument(
        "variant",
        nargs="?",
        choices=["live", "installed", "all"],
        help="for `build rootfs`: which image (default all)",
    )
    build.add_argument(
        "--debug-info",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="build the kernel with debug info (default: yes; --no-debug-info is much faster)",
    )
    build.add_argument(
        "--rebuild-image", action="store_true", help="rebuild the container image first"
    )


def _add_doctor(subparsers: argparse._SubParsersAction) -> None:
    doctor = subparsers.add_parser(
        "doctor",
        help="validate the kernel trim and report the release, building nothing",
        description=(
            "Fetches the base config and the kernel source, applies the trim, checks\n"
            "the never-disable guard, and prints what the release will be. Answers the\n"
            "questions that otherwise cost a forty-five-minute build to get wrong."
        ),
    )
    doctor.add_argument("--rebuild-image", action="store_true")


def _add_verify(subparsers: argparse._SubParsersAction) -> None:
    subparsers.add_parser(
        "verify",
        help="check the configuration for internal contradictions (fast, offline)",
        description=(
            "Runs in a second and needs no network or kernel source. Checks that no\n"
            "symbol on the never-disable list is also disabled by the trim fragment."
        ),
    )


def _add_install(subparsers: argparse._SubParsersAction) -> None:
    from .installer.cli import DESCRIPTION, EPILOG, add_arguments

    # The installer contributes its arguments rather than a whole parser, so
    # `ubuntu-uki-iso install --help` and the standalone entry point describe the
    # same flags without either owning the other.
    install = subparsers.add_parser(
        "install",
        help=DESCRIPTION,
        description=DESCRIPTION,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=EPILOG,
    )
    add_arguments(install)


def _add_test(subparsers: argparse._SubParsersAction) -> None:
    test = subparsers.add_parser("test", help="run the test suites")
    test.add_argument("kind", choices=["unit", "vm"], help="unit tests, or QEMU")
    test.add_argument(
        "--test",
        dest="which",
        default="all",
        choices=["all", "live-boot", "refuse-blank", "dry-run", "install"],
        help="for `test vm`: which scenario (default all)",
    )


def _add_housekeeping(subparsers: argparse._SubParsersAction) -> None:
    clean = subparsers.add_parser("clean", help="remove build output")
    clean.add_argument("--images", action="store_true", help="also remove the container images")

    subparsers.add_parser(
        "fix-perms",
        help="reclaim ownership of out/ after a build was killed mid-run",
        description=(
            "Build steps run as root inside the container, so a run that was killed\n"
            "hard enough to skip the chown-on-exit can leave out/ undeletable by the\n"
            "host user — who has no sudo to fix it with. This does it from a container."
        ),
    )


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


def cmd_build(args: argparse.Namespace, layout: Layout, console: Console) -> int:
    from .build.container import ContainerRunner

    container = ContainerRunner(layout, console)
    if args.rebuild_image:
        container.build_image()
    container.ensure_image()

    if args.step == "kernel":
        if not args.debug_info:
            os.environ[f"{settings.ENV_PREFIX}DEBUG_INFO"] = "0"
        container.run_step("ubuntu_uki_iso.build.kernel")
    elif args.step == "rootfs":
        container.run_step("ubuntu_uki_iso.build.rootfs", args.variant or "all")
    elif args.step == "uki":
        container.run_step("ubuntu_uki_iso.build.uki")
    elif args.step == "squashfs":
        container.run_step("ubuntu_uki_iso.build.squashfs")
    elif args.step == "iso":
        container.run_step("ubuntu_uki_iso.build.iso")
    else:  # all
        for module, extra in (
            ("ubuntu_uki_iso.build.kernel", ()),
            ("ubuntu_uki_iso.build.rootfs", ("all",)),
            ("ubuntu_uki_iso.build.uki", ()),
            ("ubuntu_uki_iso.build.squashfs", ()),
            ("ubuntu_uki_iso.build.iso", ()),
        ):
            container.run_step(module, *extra)

    return 0


def cmd_doctor(args: argparse.Namespace, layout: Layout, console: Console) -> int:
    from .build.container import ContainerRunner

    container = ContainerRunner(layout, console)
    if args.rebuild_image:
        container.build_image()
    container.ensure_image()
    container.run_step("ubuntu_uki_iso.build.kernel", "--check-only")
    release = layout.kernel_release()
    console.step("doctor: ok")
    console.info(f"kernel release: {release}")
    return 0


def cmd_verify(args: argparse.Namespace, layout: Layout, console: Console) -> int:
    from .config import kernel as kconfig

    console.step("verify: never-disable guard vs trim fragment")

    overlap = kconfig.guard_overlap()
    if overlap:
        console.error("these guarded symbols are disabled by the trim fragment:")
        for symbol in overlap:
            console.error(f"  {symbol}")
        console.error("")
        console.error("These would break containers or storage.")
        console.error("Fix ubuntu_uki_iso/data/kernel/config-trim.fragment.")
        return 1

    entries = kconfig.parse_guard()
    console.ok(f"{len(entries)} guarded symbols, none of them trimmed")

    unknown = kconfig.unknown_fragment_symbols(
        _base_config_text(layout, console), kconfig.trim_fragment()
    )
    if unknown:
        for symbol in sorted(unknown):
            console.warn(f"trim fragment mentions {symbol}, which is not a kernel symbol")
        console.warn("these lines do nothing; check the spelling")
    else:
        console.ok("every trim symbol exists in the base config")

    return 0


def _base_config_text(layout: Layout, console: Console) -> str:
    """The base config, if a build has already fetched it.

    Absent on a fresh checkout, and that is fine — the symbol-existence check
    simply cannot run yet, which is reported rather than hidden.
    """
    path = layout.kernel / "base.config"
    if path.is_file():
        return path.read_text(errors="replace")
    console.grey("no base config yet; run `ubuntu-uki-iso doctor` for the full check")
    return ""


def cmd_install(args: argparse.Namespace, layout: Layout, console: Console) -> int:
    """Hand off to the installer.

    The signature matches the dispatch table, which passes every command the
    same three arguments. The installer does not use the workspace Layout —
    it runs on a machine, not in a checkout — but taking it keeps the table
    uniform, and a command that quietly opted out of it would be the one that
    fails at the first `--apply`.
    """
    from .installer import cli as installer_cli

    return installer_cli.install(args, console)


def cmd_test(args: argparse.Namespace, layout: Layout, console: Console) -> int:
    if args.kind == "unit":
        return _run_pytest(console)
    return _test_vm(args, layout, console)


def _run_pytest(console: Console) -> int:
    import subprocess

    console.step("unit tests")
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q"],
        cwd=Layout().root,
        check=False,
    )
    return result.returncode


def _test_vm(args: argparse.Namespace, layout: Layout, console: Console) -> int:
    from .build.container import ContainerRunner
    from .qemu import run_tests

    if layout.iso.is_file() and os.environ.get("UBUNTU_UKI_ISO_NO_CONTAINER") == "1":
        return run_tests(args.which, layout, console=console)

    container = ContainerRunner(layout, console)
    container.ensure_image("test")
    container.run_step(
        "ubuntu_uki_iso.qemu",
        args.which,
        image=settings.TEST_IMAGE,
    )
    return 0


def cmd_clean(args: argparse.Namespace, layout: Layout, console: Console) -> int:
    from .build.container import ContainerRunner

    container = ContainerRunner(layout, console)
    container.fix_perms()
    container.remove_tree(layout.out)
    console.info("removed out/")

    if args.images:
        import subprocess

        for image in (settings.BUILD_IMAGE, settings.TEST_IMAGE):
            subprocess.run(
                [settings.DOCKER, "rmi", image],
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        console.info("removed the container images")
    return 0


def cmd_fix_perms(args: argparse.Namespace, layout: Layout, console: Console) -> int:
    from .build.container import ContainerRunner

    ContainerRunner(layout, console).fix_perms()
    return 0


def cmd_mark_booted(args: argparse.Namespace, layout: Layout, console: Console) -> int:
    """Record that this kernel reached multi-user.

    Called by ``xiahualab-mark-booted.service``. ``ubuntu-uki-iso install --bless``
    refuses to act without this file, which is what stops a UKI that has never
    booted from replacing the firmware fallback.
    """
    from .installer.efientry import BOOT_MARKER

    BOOT_MARKER.parent.mkdir(parents=True, exist_ok=True)
    BOOT_MARKER.write_text(os.uname().release + "\n", encoding="utf-8")
    return 0


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

_COMMANDS = {
    "build": cmd_build,
    "doctor": cmd_doctor,
    "verify": cmd_verify,
    "install": cmd_install,
    "test": cmd_test,
    "clean": cmd_clean,
    "fix-perms": cmd_fix_perms,
    "mark-booted": cmd_mark_booted,
}


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    parser = build_parser()
    args = parser.parse_args(argv)

    if not args.command:
        parser.print_help()
        return 0

    # colour=None means auto-detect from the stream, which is what we want
    # when output is being piped into a build log. --no-colour forces it off.
    console = Console(colour=False if args.no_colour else None, log_path=args.log)
    set_console(console)
    layout = Layout()

    try:
        return int(_COMMANDS[args.command](args, layout, console) or 0)
    except Refusal as exc:
        console.refuse(exc.reason)
        return Refusal.exit_code
    except XiahualabError as exc:
        console.error(str(exc))
        return 1
    except KeyboardInterrupt:
        console.error("interrupted")
        return 130
    finally:
        console.close()


if __name__ == "__main__":
    sys.exit(main())
