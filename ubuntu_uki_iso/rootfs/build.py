"""
Build a shared Ubuntu base and the live-only SquashFS difference layer.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from .. import settings
from ..boot_package import extract_package
from ..errors import BuildError, ConfigError
from ..installer import package as installer_package
from ..media import digest
from ..settings import Mode
from ..uki import config as boot
from . import config
from .folders import FolderLayout
from .layers import difference


def required_space_gb() -> int:
    """
    Return the minimum free disk space for a rootfs and its compressed image.
    """
    return 10


def preflight(temp_dir: Path) -> None:
    """
    Check dependencies, privileges and disk space before creating build files.

    Args:
        temp_dir: Directory whose filesystem will hold the rootfs artifacts.

    Returns:
        None. Raises BuildError if a prerequisite is missing.
    """
    packages = config.required_packages()
    query = subprocess.run(
        ["apt", "list", "--installed", *packages],
        capture_output=True,
        text=True,
        check=False,
    )
    if query.returncode:
        raise BuildError(f"apt could not inspect installed packages: {query.stderr.strip()}")
    installed = {line.partition("/")[0] for line in query.stdout.splitlines() if "/" in line}
    missing = [package for package in packages if package not in installed]
    if missing:
        raise BuildError(
            f"missing packages: {', '.join(missing)}\n"
            f"install with: apt-get install -y {' '.join(missing)}"
        )
    if os.geteuid() != 0:
        raise BuildError(
            "rootfs builds require root to preserve package ownership; run with sudo or in the build container"
        )
    probe = temp_dir.resolve()
    while not probe.exists() and probe.parent != probe:
        probe = probe.parent
    available = shutil.disk_usage(probe).free // (1024**3)
    if available < required_space_gb():
        raise BuildError(
            f"need about {required_space_gb()} GB free under {temp_dir}, have {available} GB"
        )
    print(f"disk: {available} GB available")


def install_boot_package(boot_deb: Path, root: Path, scratch: Path, mode: Mode) -> str:
    """
    Validate and install the boot package into the target package database.
    """
    extracted = scratch / "boot-package"
    release = extract_package(boot_deb, extracted, mode)
    environment = os.environ.copy()
    environment["UBUNTU_UKI_REGISTER_EFI"] = "0"
    subprocess.run(
        ["dpkg", f"--root={root}", f"--log={root}/var/log/dpkg.log", "-i", boot_deb],
        check=True,
        env=environment,
    )
    subprocess.run(["depmod", "-b", root, release], check=True)
    return release


def customize(root: Path, mode: Mode) -> None:
    """
    Configure services and remove machine-specific state from a bootstrapped rootfs.

    Args:
        root: New rootfs tree, with package maintainer scripts already completed.
        mode: Image variant, either live or installed.

    Returns:
        None. Commands operate on the target tree without starting host services.
    """
    # Both: select the services this image should start when it boots.
    units = config.services(mode)
    print(f"==> customize ({mode})")
    # Both: give the image a hostname; the live suffix distinguishes the installer.
    hostname = f"{settings.BOOT_LABEL}-live" if mode == Mode.LIVE else settings.BOOT_LABEL
    files = {
        "etc/hostname": hostname + "\n",
        # Both: resolve localhost and the image hostname without external DNS.
        "etc/hosts": f"127.0.0.1 localhost\n127.0.1.1 {hostname}\n::1 localhost ip6-localhost\n",
        # Both: record the same mode-specific boot arguments embedded in the UKI.
        "etc/kernel/cmdline": boot.cmdline(mode) + "\n",
        # Both: let NetworkManager manage interfaces through Netplan.
        "etc/netplan/01-network-manager-all.yaml": (
            "network:\n  version: 2\n  renderer: NetworkManager\n"
        ),
        # Both: send DNS settings learned by NetworkManager to systemd-resolved.
        "etc/NetworkManager/conf.d/10-dns-resolved.conf": ("[main]\ndns=systemd-resolved\n"),
    }
    if mode == Mode.LIVE:
        # Live only: open root sessions on the local and serial installer consoles.
        for unit in ("getty@tty1.service", "serial-getty@ttyS0.service"):
            files[f"etc/systemd/system/{unit}.d/autologin.conf"] = (
                "[Service]\nExecStart=\nExecStart=-/sbin/agetty --autologin root --noclear %I $TERM\n"
            )
    else:
        # Installed only: generate missing SSH host keys when the system boots.
        # The keys persist on disk, so ssh-keygen -A leaves existing keys intact.
        files["etc/systemd/system/ssh-host-keys.service"] = (
            "[Unit]\nDescription=Generate unique SSH host keys\nBefore=ssh.service\n"
            "[Service]\nType=oneshot\nExecStart=/usr/bin/ssh-keygen -A\nRemainAfterExit=yes\n"
            "[Install]\nWantedBy=multi-user.target\n"
        )
        # Installed only: require key generation to finish before SSH starts.
        files["etc/systemd/system/ssh.service.d/host-keys.conf"] = (
            "[Unit]\nRequires=ssh-host-keys.service\nAfter=ssh-host-keys.service\n"
        )
    # Both: write configuration into the target tree, with private Netplan files.
    for relative, content in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        path.chmod(0o600 if relative.startswith("etc/netplan/") else 0o644)
    if mode == Mode.LIVE:
        # Live only: embed the offline installer venv and its command symlink.
        installer_package.install(root)
    # Both: prevent networkd and socket activation from competing with NetworkManager.
    subprocess.run(
        [
            "systemctl",
            f"--root={root}",
            "mask",
            "systemd-networkd.service",
            "systemd-networkd.socket",
            "systemd-networkd-wait-online.service",
            *(("mdmonitor.service",) if mode == Mode.LIVE else ()),
        ],
        check=True,
    )
    if mode == Mode.LIVE:
        # The common server packages ship enabled units; live starts only its console/network.
        subprocess.run(
            [
                "systemctl",
                f"--root={root}",
                "disable",
                "docker.service",
                "docker.socket",
                "containerd.service",
                "ssh.service",
                "ssh.socket",
            ],
            check=True,
        )
    # Both: enable services in the image without starting any on the build host.
    # Live adds the serial console; installed adds Docker, SSH and RAID monitoring.
    subprocess.run(["systemctl", f"--root={root}", "enable", *units], check=True)
    # Both: clear identities created during bootstrap so each booted image can
    # obtain its own machine ID instead of inheriting the build environment's ID.
    for relative in ("etc/machine-id", "var/lib/dbus/machine-id"):
        path = root / relative
        path.unlink(missing_ok=True)
    (root / "etc/machine-id").write_text("", encoding="utf-8")
    # Both: remove generated SSH keys so images never distribute a shared identity.
    # The installed profile regenerates its keys through the service above.
    for path in (root / "etc/ssh").glob("ssh_host_*_key*"):
        path.unlink()
    # Both: route application DNS queries through the systemd-resolved stub.
    resolv = root / "etc/resolv.conf"
    resolv.unlink(missing_ok=True)
    resolv.symlink_to("../run/systemd/resolve/stub-resolv.conf")
    # Both: discard downloaded package metadata, cached debs, build logs and
    # temporary files before retaining and compressing the filesystem.
    for relative in ("var/lib/apt/lists", "var/cache/apt/archives", "var/log", "tmp", "var/tmp"):
        directory = root / relative
        if directory.is_dir():
            for path in directory.iterdir():
                if path.is_dir() and not path.is_symlink():
                    shutil.rmtree(path)
                else:
                    path.unlink()


def squash(source: Path, destination: Path, jobs: int) -> Path:
    """
    Compress a rootfs, preserving ownership and publishing only a verified image.

    Args:
        source: Customized rootfs directory.
        destination: SquashFS image to create or replace.
        jobs: Number of compression worker threads.

    Returns:
        Path to the image after unsquashfs verifies its superblock.
    """
    if jobs < 1:
        raise BuildError("compression jobs must be positive")
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="squashfs-", dir=destination.parent) as scratch:
        candidate = Path(scratch) / "image.squashfs"
        argv = [
            "mksquashfs",
            str(source),
            str(candidate),
            "-comp",
            "zstd",
            "-Xcompression-level",
            str(config.compression_level()),
            "-processors",
            str(jobs),
            "-noappend",
            "-no-progress",
            "-wildcards",
        ]
        environment = os.environ.copy()
        epoch = environment.pop("SOURCE_DATE_EPOCH", None)
        if epoch:
            argv += ["-all-time", epoch, "-mkfs-time", epoch]
        argv += ["-e", *config.squashfs_excludes()]
        print(f"==> squashfs: {destination}")
        subprocess.run(argv, check=True, env=environment)
        subprocess.run(["unsquashfs", "-s", candidate], check=True)
        candidate.replace(destination)
    return destination


def build(
    boot_deb: Path,
    temp_dir: Path,
    mode: Mode = Mode.INSTALLED,
    jobs: int = 1,
) -> Path:
    """
    Build the installed shared base or a live layer against that base.

    Args:
        boot_deb: Mode-specific boot package containing the UKI and matching modules.
        temp_dir: Directory holding boot packages and rootfs/SquashFS outputs.
        mode: Image variant, either live or installed.
        jobs: Number of SquashFS compression workers.

    Returns:
        Path to the base or live-layer SquashFS. The full customized tree is retained.
    """
    packages = config.package_list(mode)
    if jobs < 1:
        raise BuildError("compression jobs must be positive")
    boot_deb = boot_deb.resolve()
    if not boot_deb.is_file():
        raise BuildError(f"boot package does not exist: {boot_deb}")
    layout = FolderLayout(temp_dir)
    if mode == Mode.LIVE and not layout.squashfs(Mode.INSTALLED).is_file():
        raise BuildError("build the installed shared base before the live installer layer")
    layout.require_dirs()
    with tempfile.TemporaryDirectory(prefix=f"rootfs-{mode}-", dir=layout.root) as scratch:
        stage = Path(scratch)
        root = stage / "rootfs"
        print(f"==> mmdebstrap ({mode})")
        subprocess.run(
            [
                "mmdebstrap",
                "--mode=root",
                "--format=directory",
                f"--arch={settings.DPKG_ARCH}",
                "--variant=minbase",
                f"--components={config.UBUNTU_COMPONENTS}",
                f"--include={packages.comma_joined()}",
                '--aptopt=Apt::Install-Recommends "false"',
                config.UBUNTU_SUITE,
                str(root),
                config.UBUNTU_MIRROR,
            ],
            check=True,
        )
        if not (root / "usr/lib/os-release").is_file():
            raise BuildError("mmdebstrap produced no Ubuntu os-release metadata")
        install_boot_package(boot_deb, root, stage, mode)
        customize(root, mode)
        source = root
        if mode == Mode.LIVE:
            base = stage / "base"
            subprocess.run(
                ["unsquashfs", "-no-progress", "-d", base, layout.squashfs(Mode.INSTALLED)],
                check=True,
            )
            if (base / "usr/lib/ubuntu-uki-iso/release").read_text().strip() != (
                root / "usr/lib/ubuntu-uki-iso/release"
            ).read_text().strip():
                raise BuildError("shared base and live layer have different kernel releases")
            source = stage / "layer"
            difference(base, root, source)
            shutil.copy2(
                root / "usr/lib/ubuntu-uki-iso/release", source / "usr/lib/ubuntu-uki-iso/release"
            )
            (source / "usr/lib/ubuntu-uki-iso/base-sha256").write_text(
                digest(layout.squashfs(Mode.INSTALLED)) + "\n"
            )
        output = squash(source, layout.squashfs(mode), jobs)
        retained = layout.rootfs(mode)
        if retained.exists():
            shutil.rmtree(retained)
        root.replace(retained)
    return output


def main(argv: list[str] | None = None) -> int:
    """
    Parse arguments and build the selected rootfs images and their SquashFS artifacts.

    Args:
        argv: Command-line arguments, or None to use sys.argv[1:].

    Returns:
        Exit status: 0 on success or 1 on a handled build failure.
    """
    parser = argparse.ArgumentParser(description="Build Ubuntu rootfs and SquashFS images")
    parser.add_argument("--temp-dir", type=Path, required=True)
    parser.add_argument("--boot-deb", type=Path, required=True, help="boot package to install")
    parser.add_argument("--mode", type=Mode, choices=list(Mode), default=Mode.INSTALLED)
    parser.add_argument("--jobs", type=int, default=os.cpu_count() or 1)
    args = parser.parse_args(argv)
    if args.jobs < 1:
        parser.error("--jobs must be positive")
    try:
        preflight(args.temp_dir)
        output = build(args.boot_deb, args.temp_dir, args.mode, args.jobs)
        print(f"==> {args.mode}: done ({output})")
        return 0
    except (BuildError, ConfigError, OSError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    except subprocess.CalledProcessError as exc:
        command = subprocess.list2cmdline([str(arg) for arg in exc.cmd])
        print(f"ERROR: command failed ({exc.returncode}): {command}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
