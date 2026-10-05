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
import shutil
import sys

from . import __version__
from .errors import (
    BuildError,
    CommandFailed,
    ConfigError,
    NotConfirmed,
    PackagesMissing,
    Refusal,
    ToolMissing,
)
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
        description=(
            "Steps run directly in the environment this command is launched in, which is\n"
            "expected to be the build container (build/Dockerfile) — the container is\n"
            "provided to the project, not managed by it."
        ),
    )
    build.add_argument(
        "step",
        choices=["kernel", "rootfs", "uki", "uki-target", "squashfs", "iso", "all"],
        help="which step to run",
    )
    build.add_argument(
        "variant",
        nargs="?",
        choices=["live", "installed", "all"],
        help="for `build rootfs`: which image (default all)",
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
    subparsers.add_parser("clean", help="remove build output")


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


def cmd_build(args: argparse.Namespace, layout: Layout, console: Console) -> int:
    from .build import run_step

    if args.step == "kernel":
        run_step("ubuntu_uki_iso.kernel.build", root=layout.root)
    elif args.step == "rootfs":
        run_step("ubuntu_uki_iso.build.rootfs", args.variant or "all", root=layout.root)
    elif args.step == "uki":
        run_step("ubuntu_uki_iso.uki.live", root=layout.root)
    elif args.step == "uki-target":
        run_step("ubuntu_uki_iso.uki.target", root=layout.root)
    elif args.step == "squashfs":
        run_step("ubuntu_uki_iso.build.squashfs", root=layout.root)
    elif args.step == "iso":
        run_step("ubuntu_uki_iso.build.iso", root=layout.root)
    else:  # all
        for module, extra in (
            ("ubuntu_uki_iso.kernel.build", ()),
            ("ubuntu_uki_iso.build.rootfs", ("all",)),
            ("ubuntu_uki_iso.uki.live", ()),
            # The target's UKI needs only the kernel packages and the packaged
            # data — not either rootfs — but it belongs here, between the UKI
            # the ISO boots and the images that are squashed.
            ("ubuntu_uki_iso.uki.target", ()),
            ("ubuntu_uki_iso.build.squashfs", ()),
            ("ubuntu_uki_iso.build.iso", ()),
        ):
            run_step(module, *extra, root=layout.root)

    return 0


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
    from .qemu import run_tests

    return run_tests(args.which, layout, console=console)


def cmd_clean(args: argparse.Namespace, layout: Layout, console: Console) -> int:
    if layout.out.exists():
        try:
            shutil.rmtree(layout.out)
        except OSError as exc:
            # Every build step runs as root inside the container, so out/ can be
            # root-owned here. The fix is to remove it as the same user that
            # wrote it — from inside that container.
            raise BuildError(
                f"could not remove {layout.out}: {exc}\n"
                "If it was written by a build inside a container, run `clean` from inside\n"
                "that container instead."
            ) from exc
    console.info("removed out/")
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
    "install": cmd_install,
    "test": cmd_test,
    "clean": cmd_clean,
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
    except (
        BuildError,
        CommandFailed,
        ConfigError,
        NotConfirmed,
        PackagesMissing,
        ToolMissing,
    ) as exc:
        console.error(str(exc))
        return 1
    except KeyboardInterrupt:
        console.error("interrupted")
        return 130
    finally:
        console.close()


if __name__ == "__main__":
    sys.exit(main())
