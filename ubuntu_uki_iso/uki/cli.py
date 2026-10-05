"""Standalone UKI command."""

from __future__ import annotations

import argparse
from pathlib import Path

from ..errors import BuildError, ConfigError, PackagesMissing, ToolMissing
from ..log import get_console
from ..paths import Layout
from ..proc import Runner
from .builder import build, preflight


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build dracut initramfs and UKI from a kernel deb")
    parser.add_argument("--preflight", action="store_true", help="check prerequisites only")
    parser.add_argument(
        "--kernel-deb", type=Path, help="default: image deb in workspace out/kernel"
    )
    parser.add_argument("--mode", choices=("live", "installed"), default="live")
    parser.add_argument("--cmdline", help="override the embedded kernel command line")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--workspace", type=Path)
    args = parser.parse_args(argv)
    console = get_console()
    runner = Runner(dry_run=False, console=console)
    try:
        preflight(runner)
        if args.preflight:
            console.ok("UKI prerequisites available")
            return 0
        layout = Layout(args.workspace)
        image = args.kernel_deb or layout.kernel_image_deb()
        output = args.output or (
            layout.uki_file if args.mode == "live" else layout.uki / "installed.efi"
        )
        build(image, output, runner, mode=args.mode, cmdline=args.cmdline)
        console.ok(f"UKI: {output}")
        return 0
    except (BuildError, ConfigError, PackagesMissing, ToolMissing, OSError) as exc:
        console.error(str(exc))
        return 1
