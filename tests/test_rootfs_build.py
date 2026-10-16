"""
Rootfs isolation, runtime modules and actual SquashFS artifacts.
"""

import os
import shutil
import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import Mock

import pytest

from helpers import make_pe
from ubuntu_uki_iso.boot_package import EFI_PATH, METADATA
from ubuntu_uki_iso.errors import BuildError
from ubuntu_uki_iso.rootfs import build, config
from ubuntu_uki_iso.rootfs.folders import FolderLayout
from ubuntu_uki_iso.settings import Mode
from ubuntu_uki_iso.uki.config import cmdline


def bootstrap_tree(root: Path) -> None:
    (root / "usr/lib").mkdir(parents=True)
    (root / "usr/lib/os-release").write_text("ID=ubuntu\n")
    (root / "lib").symlink_to("usr/lib")
    (root / "etc/ssh").mkdir(parents=True)
    (root / "etc/machine-id").write_text("host-machine-id")
    (root / "etc/ssh/ssh_host_ed25519_key").write_text("shared-key")
    (root / "etc/ssh/ssh_host_ed25519_key.pub").write_text("shared-public-key")
    (root / "etc/resolv.conf").write_text("nameserver 1.2.3.4\n")


@pytest.mark.parametrize("mode", [Mode.LIVE, Mode.INSTALLED])
def test_build_installs_boot_package_and_customizes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: Mode
) -> None:
    deb = tmp_path / "image.deb"
    deb.touch()
    commands = []

    def run(argv: list[Any], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        args = [str(arg) for arg in argv]
        commands.append(args)
        assert kwargs["check"] is True
        if args[0] == "mksquashfs":
            assert "SOURCE_DATE_EPOCH" not in kwargs["env"]
        elif args[0] == "dpkg":
            assert kwargs["env"]["UBUNTU_UKI_REGISTER_EFI"] == "0"
        else:
            assert kwargs == {"check": True}
        if args[0] == "mmdebstrap":
            assert "--mode=root" in args
            assert "--variant=minbase" in args
            bootstrap_tree(Path(args[-2]))
        elif args[0] == "unsquashfs" and "-d" in args:
            base = Path(args[args.index("-d") + 1])
            (base / METADATA).mkdir(parents=True)
            (base / METADATA / "release").write_text("7.0-test")
        elif args[0] in ("dpkg-deb", "dpkg"):
            root = Path(args[-1]) if args[0] == "dpkg-deb" else Path(args[1].split("=", 1)[1])
            modules = root / "usr/lib/modules/7.0-test"
            modules.mkdir(parents=True)
            (modules / "nvme.ko").write_bytes(b"module")
            (root / EFI_PATH).parent.mkdir(parents=True)
            (root / EFI_PATH).write_bytes(
                make_pe(
                    {
                        ".linux": b"kernel",
                        ".initrd": b"initrd",
                        ".uname": b"7.0-test",
                        ".cmdline": cmdline(mode).encode(),
                    }
                )
            )
            (root / METADATA).mkdir(parents=True)
            (root / METADATA / "release").write_text("7.0-test")
            (root / METADATA / "mode").write_text(mode)
        elif args[0] == "/usr/bin/python3":
            wheels = Path(args[args.index("--wheel-dir") + 1])
            wheels.mkdir()
            (wheels / "installer.whl").write_bytes(b"wheel")
        elif args[0] == "mksquashfs":
            Path(args[2]).write_bytes(b"verified image")
        return subprocess.CompletedProcess(args, 0)

    monkeypatch.setattr(build.subprocess, "run", run)
    if mode == Mode.LIVE:
        base_image = FolderLayout(tmp_path / "out").squashfs(Mode.INSTALLED)
        base_image.parent.mkdir(parents=True)
        base_image.touch()
        monkeypatch.setattr(
            build,
            "difference",
            lambda base, root, layer: shutil.copytree(root, layer, symlinks=True),
        )
    output = build.build(deb, tmp_path / "out", mode, 2)
    layout = FolderLayout(tmp_path / "out")
    root = layout.rootfs(mode)
    assert output == layout.squashfs(mode)
    assert output.read_bytes() == b"verified image"
    assert (root / "usr/lib/modules/7.0-test/nvme.ko").read_bytes() == b"module"
    assert not (root / "usr/lib/modules/7.0-test/build").is_symlink()
    assert not (root / "boot/vmlinuz-7.0-test").exists()
    install = next(args for args in commands if args[0] == "dpkg")
    assert install[1].startswith("--root=")
    target = install[1].split("=", 1)[1]
    assert install[2:] == [f"--log={target}/var/log/dpkg.log", "-i", str(deb)]
    assert (root / "etc/machine-id").read_text() == ""
    assert not list((root / "etc/ssh").glob("ssh_host_*"))
    assert (root / "etc/resolv.conf").is_symlink()
    units = next(args for args in commands if args[0] == "systemctl" and args[2] == "enable")
    assert units[1].startswith("--root=")
    assert "NetworkManager.service" in units
    assert "systemd-networkd.service" not in units
    masked = next(args for args in commands if args[0] == "systemctl" and args[2] == "mask")
    assert masked[1] == units[1]
    assert set(masked[3:]) == {
        "systemd-networkd.service",
        "systemd-networkd.socket",
        "systemd-networkd-wait-online.service",
    } | ({"mdmonitor.service"} if mode == Mode.LIVE else set())
    disabled = [args for args in commands if args[0] == "systemctl" and args[2] == "disable"]
    if mode == Mode.LIVE:
        assert len(disabled) == 1
        assert set(disabled[0][3:]) == {
            "docker.service",
            "docker.socket",
            "containerd.service",
            "ssh.service",
            "ssh.socket",
        }
        assert not {"docker.service", "ssh.service", "mdmonitor.service"} & set(units)
    else:
        assert not disabled
        assert {"docker.service", "ssh.service", "mdmonitor.service"} <= set(units)
    netplan = root / "etc/netplan/01-network-manager-all.yaml"
    assert "renderer: NetworkManager" in netplan.read_text()
    assert netplan.stat().st_mode & 0o777 == 0o600
    assert not (root / "etc/systemd/network/20-wired.network").exists()
    assert (
        "dns=systemd-resolved"
        in (root / "etc/NetworkManager/conf.d/10-dns-resolved.conf").read_text()
    )
    if mode == Mode.INSTALLED:
        assert "ssh-host-keys.service" in units
        assert not (root / "etc/systemd/system/getty@tty1.service.d/autologin.conf").exists()
        assert "root=PARTLABEL=" in (root / "etc/kernel/cmdline").read_text()
    else:
        assert (root / "etc/systemd/system/getty@tty1.service.d/autologin.conf").is_file()
    assert not (root / "usr/local/lib/ubuntu-uki-iso").exists()
    chroots = [args for args in commands if args[0] == "chroot"]
    launcher = root / "usr/local/bin/ubuntu-uki-install"
    if mode == Mode.LIVE:
        assert launcher.is_symlink()
        assert launcher.readlink() == Path("/opt/ubuntu-uki-installer/bin/ubuntu-uki-install")
        assert chroots[0][2:] == ["python3", "-m", "venv", "/opt/ubuntu-uki-installer"]
        assert chroots[1][2:6] == ["/opt/ubuntu-uki-installer/bin/python", "-m", "pip", "install"]
        assert "--no-index" in chroots[1] and "--no-deps" in chroots[1]
        assert chroots[1][-1].startswith("/tmp/installer-")
        assert not list((root / "tmp").iterdir())
    else:
        assert not chroots
        assert not launcher.is_symlink()
        assert not (root / "opt/ubuntu-uki-installer").exists()
    assert not list(layout.root.glob(f"rootfs-{mode}-*"))


@pytest.mark.parametrize("mode", [Mode.LIVE, Mode.INSTALLED])
def test_profiles_do_not_install_boot_build_tools(mode: Mode) -> None:
    packages = set(config.package_list(mode))
    assert not packages & {
        "dracut",
        "systemd-ukify",
        "systemd-boot-efi",
        "python3-setuptools",
        "python3-wheel",
    }
    assert ("python3-venv" in packages) == (mode == Mode.LIVE)
    assert {"kmod", "systemd", "systemd-resolved", "netplan.io", "network-manager"} <= packages


@pytest.mark.skipif(shutil.which("mksquashfs") is None, reason="squashfs-tools required")
def test_real_squashfs_preserves_metadata_and_excludes_transient_contents(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "rootfs"
    source.mkdir()
    for directory in ("proc", "sys", "dev", "run", "tmp", "var/tmp", "boot/efi"):
        (source / directory).mkdir(parents=True, exist_ok=True)
        (source / directory / "runtime-file").write_text("exclude me")
    (source / "etc").mkdir()
    file = source / "etc/test"
    file.write_text("preserve me")
    file.chmod(0o640)
    monkeypatch.setenv("SOURCE_DATE_EPOCH", "1700000000")
    output = build.squash(source, tmp_path / "one.squashfs", 2)
    second = build.squash(source, tmp_path / "two.squashfs", 2)
    assert output.read_bytes() == second.read_bytes()
    extracted = tmp_path / "extracted"
    subprocess.run(
        ["unsquashfs", "-d", str(extracted), str(output)], check=True, capture_output=True
    )
    result = extracted / "etc/test"
    assert result.read_text() == "preserve me"
    assert result.stat().st_mode & 0o777 == 0o640
    assert result.stat().st_uid == os.getuid()
    for directory in ("proc", "sys", "dev", "run", "tmp", "var/tmp", "boot/efi"):
        assert (extracted / directory).is_dir()
        assert not list((extracted / directory).iterdir())


def test_compression_failure_preserves_previous_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "rootfs"
    source.mkdir()
    output = tmp_path / "image.squashfs"
    output.write_bytes(b"previous")
    monkeypatch.setattr(
        build.subprocess, "run", Mock(side_effect=subprocess.CalledProcessError(1, ["mksquashfs"]))
    )
    with pytest.raises(subprocess.CalledProcessError):
        build.squash(source, output, 1)
    assert output.read_bytes() == b"previous"
    assert not list(tmp_path.glob("squashfs-*"))


def test_cli_requires_boot_package(tmp_path: Path) -> None:
    with pytest.raises(SystemExit) as exc:
        build.main(["--temp-dir", str(tmp_path)])
    assert exc.value.code == 2


def test_preflight_failure_does_not_create_directories(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(build, "preflight", Mock(side_effect=BuildError("missing mmdebstrap")))
    directory = tmp_path / "new"
    assert build.main(["--temp-dir", str(directory), "--boot-deb", "boot.deb"]) == 1
    assert not directory.exists()


def test_cli_explicit_boot_package(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    deb = tmp_path / "custom.deb"
    monkeypatch.setattr(build, "preflight", Mock())
    compile_image = Mock(return_value=tmp_path / "image.squashfs")
    monkeypatch.setattr(build, "build", compile_image)
    assert (
        build.main(
            [
                "--temp-dir",
                str(tmp_path),
                "--boot-deb",
                str(deb),
                "--jobs",
                "2",
            ]
        )
        == 0
    )
    compile_image.assert_called_once_with(deb, tmp_path, Mode.INSTALLED, 2)


def test_cli_rejects_one_package_for_both_modes(tmp_path: Path) -> None:
    with pytest.raises(SystemExit):
        build.main(
            [
                "--temp-dir",
                str(tmp_path),
                "--boot-deb",
                "boot.deb",
                "--mode",
                "all",
            ]
        )


def test_live_layer_requires_installed_base_before_bootstrap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    deb = tmp_path / "boot-live.deb"
    deb.touch()
    run = Mock()
    monkeypatch.setattr(build.subprocess, "run", run)
    with pytest.raises(BuildError, match="shared base"):
        build.build(deb, tmp_path / "out", Mode.LIVE)
    run.assert_not_called()


def test_rootfs_excludes_external_module_build_dependencies() -> None:
    for mode in Mode:
        assert not {"dkms", "build-essential", "libelf-dev"} & set(config.package_list(mode))
