"""
Boot a hybrid ISO as USB storage under OVMF and exercise the real installer.

Only qcow2 files are attached: one 1 TB NVMe SSD and eight 8 TB SATA HDDs.
Guest scripts fail on unsuccessful checks; serial transcripts remain in vm-logs.
"""

from __future__ import annotations

import argparse
import base64
import os
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

from ..errors import BuildError, ConfigError, ToolMissing
from ..installer import config as storage
from ..settings import BOOT_LABEL, ROOT_LABEL
from .config import (
    AHCI_PORTS,
    VM_BOOT_TIMEOUT,
    VM_COMMAND_TIMEOUT,
    VM_CPUS,
    VM_DATA_DISK_SIZE,
    VM_DATA_DISKS,
    VM_MEMORY_MB,
    VM_ROOT_DISK_SIZE,
)
from .folders import FolderLayout

QEMU = "qemu-system-x86_64"
OVMF_PAIRS = (
    ("/usr/share/OVMF/OVMF_CODE_4M.fd", "/usr/share/OVMF/OVMF_VARS_4M.fd"),
    ("/usr/share/OVMF/OVMF_CODE.fd", "/usr/share/OVMF/OVMF_VARS.fd"),
)
TEST_NAMES = ("live-boot", "refuse-blank", "create-plan", "dry-run", "install")
# Public credentials only for the disposable guest; never used on the host.
TEST_PASSWORD = "qemu-test-only"
DONE = "UBUNTU_UKI_TEST_DONE"


def _tcl_word(word: str) -> str:
    """
    Quote a literal Tcl argument, including paths containing spaces.
    """
    return (
        '"' + re.sub(r'([\\"$\[\]])', r"\\\1", word).replace("\n", "\\n").replace("\r", "\\r") + '"'
    )


class Harness:
    """
    Keep virtual disk files and firmware state across installation and reboot.
    """

    def __init__(self, layout: FolderLayout) -> None:
        self.layout = layout
        self.accel = "kvm" if os.access("/dev/kvm", os.R_OK | os.W_OK) else "tcg"
        self.ovmf_code = ""
        self.ovmf_vars = ""
        self.root_image = layout.vm / "root.qcow2"
        self.data_images = tuple(layout.vm / f"data{i}.qcow2" for i in range(VM_DATA_DISKS))

    def prepare(self) -> None:
        if not self.layout.iso.is_file():
            raise BuildError(
                f"ISO missing: {self.layout.iso}; build it with ubuntu-uki-image first"
            )
        missing = [
            tool for tool in (QEMU, "qemu-img", "expect", "timeout") if not shutil.which(tool)
        ]
        if missing:
            raise ToolMissing(*missing)
        for code, variables in OVMF_PAIRS:
            if Path(code).is_file() and Path(variables).is_file():
                self.ovmf_code, self.ovmf_vars = code, variables
                break
        else:
            raise BuildError("Install ovmf: a matching unsigned OVMF CODE/VARS pair is required")
        self.layout.vm.mkdir(parents=True, exist_ok=True)
        self.layout.vm_logs.mkdir(parents=True, exist_ok=True)
        print(f"OVMF: {self.ovmf_code}; acceleration: {self.accel}")
        print("Storage: 1 TB NVMe SSD + eight 8 TB SATA HDDs; sparse qcow2 files")

    def _reset_disks(self) -> None:
        for image, size in (
            (self.root_image, VM_ROOT_DISK_SIZE),
            *((image, VM_DATA_DISK_SIZE) for image in self.data_images),
        ):
            image.unlink(missing_ok=True)
            subprocess.run(["qemu-img", "create", "-f", "qcow2", str(image), str(size)], check=True)

    def _qemu_argv(self, name: str, *, installed: bool = False, data: bool = True) -> list[str]:
        """
        Attach file-backed storage, explicit network/USB controllers and writable NVRAM.
        """
        argv = [
            QEMU,
            "-machine",
            f"q35,accel={self.accel}",
            "-cpu",
            "host" if self.accel == "kvm" else "max",
            "-m",
            str(VM_MEMORY_MB),
            "-smp",
            str(VM_CPUS),
            "-display",
            "none",
            "-monitor",
            "none",
            "-serial",
            "stdio",
            "-no-reboot",
            "-drive",
            f"if=pflash,format=raw,unit=0,readonly=on,file={self.ovmf_code}",
            "-drive",
            f"if=pflash,format=raw,unit=1,file={self.layout.vm / (name + '-VARS.fd')}",
            "-netdev",
            "user,id=net0",
            "-device",
            "e1000e,netdev=net0",
            "-drive",
            f"file={self.root_image},if=none,id=rootdisk,format=qcow2,discard=unmap",
            "-device",
            f"nvme,serial=ROOTDISK,drive=rootdisk,bootindex={1 if installed else 2}",
        ]
        if data:
            for index in range((VM_DATA_DISKS + AHCI_PORTS - 1) // AHCI_PORTS):
                argv += ["-device", f"ich9-ahci,id=sata{index}"]
            for index, image in enumerate(self.data_images):
                argv += [
                    "-drive",
                    f"file={image},if=none,id=d{index},format=qcow2,discard=unmap",
                    "-device",
                    f"ide-hd,drive=d{index},bus=sata{index // AHCI_PORTS}.{index % AHCI_PORTS},serial=DATA{index},rotation_rate=7200",
                ]
        if not installed:
            argv += [
                "-device",
                "qemu-xhci,id=xhci",
                "-drive",
                f"file={self.layout.iso},if=none,id=usbstick,format=raw,readonly=on",
                "-device",
                "usb-storage,drive=usbstick,removable=on,bootindex=1",
            ]
        return argv

    def _expect_script(
        self, name: str, command: str, *, installed: bool = False, data: bool = True
    ) -> str:
        """
        Transfer the script without echoed assertions, then check its actual exit status.
        """
        command = (
            'trap \'printf "FAILED line %s: %s\\n" "$LINENO" "$BASH_COMMAND" >&2\' ERR\n' + command
        )
        payload = base64.encodebytes(command.encode()).decode().strip()
        # Keep input lines below the serial terminal's canonical input limit.
        launch = (
            "cat > /tmp/vm-check.b64 <<'VM_PAYLOAD'\n"
            + payload
            + "\nVM_PAYLOAD\nbase64 -d /tmp/vm-check.b64 > /tmp/vm-check; "
            "bash -euo pipefail /tmp/vm-check; rc=$?; "
            "printf '\\nUBUNTU_UKI_TEST_%s:%s\\n' DONE $rc"
        )
        shutdown = f"printf '%s\\n' {TEST_PASSWORD} | sudo -S poweroff" if installed else "poweroff"
        lines = [
            f"set timeout {VM_BOOT_TIMEOUT}",
            "log_user 1",
            "spawn "
            + " ".join(
                _tcl_word(word) for word in self._qemu_argv(name, installed=installed, data=data)
            ),
            "expect {",
            f'  -re {{login: }} {{ send -- "{"vmtest" if installed else "root"}\\r"; exp_continue }}',
            f'  -re {{Password: }} {{ send -- "{TEST_PASSWORD}\\r"; exp_continue }}',
            "  -re {[#$] $} {}",
            "  timeout { exit 42 }",
            "  eof { exit 43 }",
            "}",
            f"set timeout {VM_COMMAND_TIMEOUT}",
            "set send_slow {128 0.01}",
            "send -s -- " + _tcl_word(launch.replace("\n", "\r") + "\r"),
            "expect {",
            f"  -re {{{DONE}:([0-9]+)}} {{ set rc $expect_out(1,string) }}",
            "  timeout { exit 44 }",
            "  eof { exit 45 }",
            "}",
            "send -- " + _tcl_word(shutdown + "\r"),
            "expect { eof {} timeout { exit 46 } }",
            "exit $rc",
            "",
        ]
        return "\n".join(lines)

    def _guest_run(
        self, name: str, command: str, *, installed: bool = False, data: bool = True
    ) -> bool:
        if not installed:
            shutil.copyfile(self.ovmf_vars, self.layout.vm / f"{name}-VARS.fd")
        script = self.layout.vm / f"{name}.exp"
        script.write_text(self._expect_script(name, command, installed=installed, data=data))
        log = (
            self.layout.vm_logs
            / f"{name}{'-installed' if installed else ''}{'-no-data' if not data else ''}.log"
        )
        print(f"Booting {name}, installed={installed}, data={data}; log: {log}", flush=True)
        with log.open("w") as stream:
            result = subprocess.run(
                [
                    "timeout",
                    str(VM_BOOT_TIMEOUT + VM_COMMAND_TIMEOUT + 60),
                    "expect",
                    "-f",
                    str(script),
                ],
                stdout=stream,
                stderr=subprocess.STDOUT,
                check=False,
            )
        text = log.read_text(errors="replace").replace("\r", "")
        ok = result.returncode == 0 and re.search(rf"^{DONE}:0$", text, re.M) is not None
        if not ok:
            lines = text.splitlines()
            end = next(
                (i + 1 for i, line in enumerate(lines) if re.fullmatch(rf"{DONE}:[0-9]+", line)),
                len(lines),
            )
            print("\n".join(lines[max(0, end - 30) : end]), file=sys.stderr)
        return ok

    @staticmethod
    def _data_disks_shell() -> str:
        """
        Discover fixture disks by serial rather than unstable sd* letters.
        """
        return "\n".join(
            [
                "udevadm settle",
                "DATA=''",
                'for link in /dev/disk/by-id/ata-*DATA*; do DATA="$DATA $(readlink -f "$link")"; done',
                f'[ "$(printf "%s\\n" $DATA | sort -u | wc -l)" -eq {VM_DATA_DISKS} ]',
            ]
        )

    def _fake_array(self) -> str:
        """
        Populate a small ext4 fixture on the full-size RAID0 to keep VM writes modest.
        """
        return "\n".join(
            [
                self._data_disks_shell(),
                f"mdadm --create /dev/md0 --level=0 --raid-devices={VM_DATA_DISKS} --chunk={storage.EXPECTED_MD_CHUNK} --metadata={storage.RAID_METADATA_VERSION} --uuid={storage.EXPECTED_MD_UUID} --run $DATA",
                # 1 GiB filesystem on the 64 TB device; geometry/discovery still see all eight 8 TB disks.
                "mkfs.ext4 -q -F -b 4096 /dev/md0 262144",
                "mkdir -p /mnt/fixture; mount /dev/md0 /mnt/fixture",
                "echo precious-data > /mnt/fixture/canary; sync; umount /mnt/fixture",
                "mdadm --stop /dev/md0",
            ]
        )

    def _checksums(self, destination: str) -> str:
        """
        Hash partition/RAID metadata regions; avoid reading 65 TB of sparse disks.
        """
        return "\n".join(
            [
                f": > {destination}",
                "for d in /dev/nvme0n1 $DATA; do",
                '  sectors=$(blockdev --getsz "$d")',
                f'  {{ dd if="$d" bs=512 count=8192 status=none; dd if="$d" bs=512 skip=$((sectors-8192)) count=8192 status=none; }} | sha256sum >> {destination}',
                "done",
            ]
        )

    def run(self, name: str) -> bool:
        self._reset_disks()
        if name == "live-boot":
            return self._guest_run(
                name,
                "\n".join(
                    [
                        "test -d /sys/firmware/efi/efivars",
                        "findmnt -n -o FSTYPE / | grep -x overlay",
                        "findmnt -n -o FSTYPE /run/initramfs/installer | grep -x squashfs",
                        "findmnt -n -o OPTIONS /run/rootfsbase | grep 'lowerdir=/run/initramfs/installer:/run/initramfs/shared-base'",
                        "test -f /run/initramfs/live/payload/boot-installed.deb",
                        "test -f /run/initramfs/live/payload/base.squashfs",
                        "lsblk -b -o NAME,SIZE,ROTA,TRAN,SERIAL",
                        self._data_disks_shell(),
                        f'test "$(blockdev --getsize64 /dev/nvme0n1)" = {VM_ROOT_DISK_SIZE}',
                        'test "$(cat /sys/block/nvme0n1/queue/rotational)" = 0',
                        f'for d in $DATA; do test "$(blockdev --getsize64 "$d")" = {VM_DATA_DISK_SIZE}; test "$(cat /sys/block/$(basename "$d")/queue/rotational)" = 1; done',
                        "lsblk -dn -o TRAN | grep -x usb",
                        "systemctl is-active NetworkManager",
                        "timeout 120 nm-online -q",
                        "ip -4 route show default | grep default",
                        'for package in docker.io openssh-server linux-firmware-nvidia-graphics; do test "$(dpkg-query -W -f=\'${db:Status-Status}\' "$package")" = installed; done',
                        'for unit in docker.service docker.socket containerd.service ssh.service ssh.socket; do test "$(systemctl is-enabled "$unit" || true)" = disabled; if systemctl is-active --quiet "$unit"; then exit 1; fi; done',
                        'test "$(systemctl is-enabled mdmonitor.service || true)" = masked',
                        "ubuntu-uki-install --help",
                    ]
                ),
            )
        if name in ("refuse-blank", "create-plan", "dry-run"):
            fixture = self._fake_array() if name == "dry-run" else self._data_disks_shell()
            invocation = "ubuntu-uki-install --dry-run"
            expected = 0
            if name == "refuse-blank":
                invocation = "ubuntu-uki-install --apply --assume-yes --raid reuse"
                expected = 2
            return self._guest_run(
                name,
                "\n".join(
                    [
                        fixture,
                        self._checksums("/tmp/before"),
                        "set +e",
                        invocation + " > /tmp/installer.log 2>&1",
                        "rc=$?",
                        "set -e",
                        "cat /tmp/installer.log",
                        f"test $rc -eq {expected}",
                        *(
                            ["grep -F 'CREATE a new raid0 array' /tmp/installer.log"]
                            if name == "create-plan"
                            else []
                        ),
                        self._checksums("/tmp/after"),
                        "cmp /tmp/before /tmp/after",
                    ]
                ),
            )
        if name == "install":
            return self._install(name)
        raise ConfigError(f"unknown VM test: {name}")

    def _install(self, name: str) -> bool:
        ssh_checks = []
        if key := storage.SSH_PUBLIC_KEY.strip():
            ssh_checks = [
                "test $(stat -c %a /mnt/target/home/vmtest/.ssh) = 700",
                "test $(stat -c %a /mnt/target/home/vmtest/.ssh/authorized_keys) = 600",
                'owner="$(chroot /mnt/target id -u vmtest):$(chroot /mnt/target id -g vmtest)"',
                'for path in /mnt/target/home/vmtest/.ssh /mnt/target/home/vmtest/.ssh/authorized_keys; do test "$(stat -c %u:%g "$path")" = "$owner"; done',
                f"grep -Fx -- {shlex.quote(key)} /mnt/target/home/vmtest/.ssh/authorized_keys",
            ]
        command = "\n".join(
            [
                self._fake_array(),
                "efibootmgr | grep -E '^(BootOrder|BootNext):' > /tmp/efi-before",
                f"umask 077; printf '%s\\n' {TEST_PASSWORD} > /run/install-password",
                "ubuntu-uki-install --apply --assume-yes --username vmtest --password-file /run/install-password",
                "rm /run/install-password",
                "efibootmgr | grep -E '^(BootOrder|BootNext):' > /tmp/efi-after",
                "cmp /tmp/efi-before /tmp/efi-after",
                f"efibootmgr -v | grep -F {shlex.quote(BOOT_LABEL)} | grep -Fi 'BOOTX64.EFI'",
                "mkdir -p /mnt/target; mount /dev/nvme0n1p2 /mnt/target",
                "mount /dev/nvme0n1p1 /mnt/target/boot/efi",
                "cmp /run/initramfs/live/payload/uki.efi /mnt/target/boot/efi/EFI/BOOT/BOOTX64.EFI",
                "chroot /mnt/target id vmtest | grep sudo",
                *ssh_checks,
                f"grep -F {shlex.quote(storage.DATA_MOUNT_OPTIONS)} /mnt/target/etc/fstab",
                "umount /mnt/target/boot/efi; umount /mnt/target",
            ]
        )
        if not self._guest_run(name, command):
            return False
        # Firmware bootindex selects NVMe explicitly; postinst never changes BootOrder/BootNext.
        checks = "\n".join(
            [
                'test "$(id -un)" = vmtest',
                "id -nG | grep -w sudo",
                "findmnt -n -o SOURCE / | grep nvme0n1p2",
                f"grep -F 'root=PARTLABEL={ROOT_LABEL}' /proc/cmdline",
                "test ! -e /run/initramfs/live/payload/base.squashfs",
                "test -d /usr/lib/modules/$(uname -r)",
                "systemctl is-active NetworkManager",
                "systemctl is-active ssh",
                "test ! -e /opt/ubuntu-uki-installer",
                "test ! -e /etc/systemd/system/getty@tty1.service.d/autologin.conf",
                "test ! -e /etc/systemd/system/serial-getty@ttyS0.service.d/autologin.conf",
                "timeout 120 nm-online -q",
                "ip -4 route show default | grep default",
            ]
        )
        if not self._guest_run(
            name, checks + "\ncat /mnt/raid0/canary | grep -x precious-data", installed=True
        ):
            return False
        return self._guest_run(name, checks + "\n! findmnt /mnt/raid0", installed=True, data=False)


def run_tests(which: str, layout: FolderLayout) -> int:
    selected = TEST_NAMES if which == "all" else (which,)
    if any(name not in TEST_NAMES for name in selected):
        raise ConfigError(f"unknown VM test: {which}")
    harness = Harness(layout)
    harness.prepare()
    failed = [name for name in selected if not harness.run(name)]
    print(f"Failed: {', '.join(failed)}" if failed else f"All {len(selected)} VM scenarios passed")
    return int(bool(failed))


def main(argv: list[str] | None = None) -> int:
    """
    Run VM checks against artifacts in the explicit build directory.
    """
    parser = argparse.ArgumentParser(description="Test a LiveUSB ISO with QEMU and OVMF")
    parser.add_argument("--temp-dir", type=Path, required=True)
    parser.add_argument("--test", choices=("all", *TEST_NAMES), default="all")
    args = parser.parse_args(argv)
    try:
        return run_tests(args.test, FolderLayout(args.temp_dir))
    except (BuildError, ConfigError, ToolMissing, OSError, subprocess.SubprocessError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
