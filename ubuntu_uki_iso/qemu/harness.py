"""Booting the built ISO in QEMU and driving it from the outside.

This is where the project's safety claims are checked against a machine rather
than asserted in a comment — on an emulated one, driven through the same
drivers the real machine uses.

Three decisions here are load-bearing.

**No real disk is ever touched.** The guest's disks are plain files under
``out/vm`` that QEMU presents through its NVMe and AHCI emulation, so the
installer inside the guest sees ``/dev/nvme0n1`` and ``/dev/sd*`` — the same
device names as the real machine — while the host creates no block device at
all. The design notes describe these as loop-backed disks; files are
deliberately stronger. A loop device on the host is a real block device the
installer could be pointed at by mistake, and the whole point of the harness is
that there is nothing here to point it at.

The guest has fewer data disks than the machine does — see
:data:`~ubuntu_uki_iso.settings.VM_DATA_DISKS`. What is being tested here is the
integration: that an array assembles on a real kernel, that the reuse path
leaves the bytes alone, and that the refusal fires. The decision table's
combinatorial cases are covered far more cheaply, and far more thoroughly, by
the pytest suite against constructed device state.

**The guest is driven by expect, and every command ends in a sentinel.**
Reading a prompt back out of a serial console is fragile — prompts change
colour, wrap, and interleave with kernel messages — whereas a unique marker is
unambiguous. The whole session is written to ``out/vm-logs/<test>.log``, so a
run that never reaches the marker can still be read afterwards.

**The evidence is taken inside the guest.** "Nothing was changed" is proved by
md5summing the disks in the guest before and after the installer runs, not by
the installer's word for it and not from the host: reading every byte back
through the emulated device layer costs more than the boot it is checking.
"""

from __future__ import annotations

import os
import re
import shutil
import stat
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from ..errors import BuildError, ConfigError
from ..log import Console, get_console
from ..paths import Layout
from ..proc import Runner
from ..settings import (
    AHCI_PORTS,
    EXPECTED_MD_CHUNK,
    EXPECTED_MD_LEVEL,
    EXPECTED_MD_UUID,
    MD_DEVICE,
    RAID_METADATA_VERSION,
    VM_BOOT_TIMEOUT,
    VM_COMMAND_TIMEOUT,
    VM_CPUS,
    VM_DATA_DISK_SIZE,
    VM_DATA_DISKS,
    VM_MEMORY_MB,
    VM_ROOT_DISK_SIZE,
    VOLID,
)

# -- the machine under test -------------------------------------------------

#: The container's qemu-system-x86 package installs this; the ISO is x86_64 and
#: so is the firmware below.
QEMU = "qemu-system-x86_64"

KVM = "/dev/kvm"

#: Firmware, in the order it is looked for. The Secure Boot build is in the
#: list only so that it can be recognised and skipped: this UKI is unsigned, so
#: a secboot firmware would refuse to run it, and a host with nothing else
#: needs to be told that rather than left to guess from a boot that hangs.
OVMF_CANDIDATES: tuple[str, ...] = (
    "/usr/share/OVMF/OVMF_CODE_4M.fd",
    "/usr/share/OVMF/OVMF_CODE.fd",
    "/usr/share/ovmf/OVMF.fd",
    "/usr/share/OVMF/OVMF_CODE_4M.secboot.fd",
)
OVMF_VARS_4M = "/usr/share/OVMF/OVMF_VARS_4M.fd"

#: The guest's root disk. NVMe, so it never collides with the sd* namespace the
#: live medium and the data disks share, and its name is safe to hardcode.
ROOT_DEVICE = "/dev/nvme0n1"

#: How many data disks the guest gets, and therefore how many disk images the
#: harness creates. The *names* are not here: the installation medium is an sd*
#: device too, so the letters move with enumeration order. The guest works out
#: which are which at runtime — see :meth:`Harness._data_disks_shell`.
#:
#: Fewer than the machine's eight because they are on AHCI and QEMU's emulated
#: controller has six ports; see :data:`~ubuntu_uki_iso.settings.VM_DATA_DISKS` for
#: why count is the right thing to compromise on.
DATA_DISK_COUNT = VM_DATA_DISKS

if VM_DATA_DISKS > AHCI_PORTS:
    raise ConfigError(
        f"VM_DATA_DISKS is {VM_DATA_DISKS}, but QEMU's emulated AHCI has "
        f"{AHCI_PORTS} ports.\n"
        "The disks would not all attach, and the failure at QEMU startup is "
        "\"Bus 'ahci.N' not found\", which says nothing about this setting.\n"
        "Either lower VM_DATA_DISKS or use a different controller in _disk_args."
    )

#: The mount point the dry-run and install tests build their fake array at.
FAKE_MOUNT = "/mnt/fake"

# mdadm numbers its levels; settings names them the way mdadm --detail prints
# them, so "raid0" has to become "0" for the fixture to match what the
# installer expects to find.
MDADM_LEVEL = EXPECTED_MD_LEVEL.removeprefix("raid")

#: What the guest prints when the command has finished, and the form the
#: command is written in so that only that print matches. The pty echoes what
#: is sent, so the sentinel appears in the output while it is still being
#: typed; the guest's shell removes the two quotes, so the echoed form differs
#: from the printed one and cannot match early.
SENTINEL = "UBUNTU_UKI_ISO-DONE"
SENTINEL_TYPED = "UBUNTU_UKI_ISO-DO''NE"

#: What expect prints when it sees the sentinel. The logs are searched for
#: this, not for the sentinel itself.
REACHED = "===UBUNTU_UKI_ISO-SENTINEL==="

#: How much of a log is shown when a guest never got as far as its command.
TAIL_LINES = 20

TEST_NAMES: tuple[str, ...] = ("live-boot", "refuse-blank", "dry-run", "install")


@dataclass(frozen=True)
class Check:
    """One assertion about a guest log.

    ``ok`` and ``fail`` are the two lines it prints, so the assertion reads the
    same here as it does on the terminal. ``evidence`` is a regex whose last
    match is shown when the check fails — for an exit code, the number the
    installer actually returned.
    """

    ok: str
    fail: str
    needle: str
    regex: bool = False
    evidence: str | None = None
    #: False for a check that reports but does not fail, which is how the shell
    #: harness treated the one message only its dry-run path printed.
    required: bool = True
    #: How many times the needle must appear. More than one turns a check into
    #: a count: "the disks were examined" is only really proved by seeing it
    #: happen once per disk, and a run that examined one and gave up would
    #: satisfy a plain substring test.
    minimum: int = 1


def _acceleration() -> str:
    """KVM where the host has it, TCG otherwise.

    TCG is the normal case on the machine this runs on, which is why the
    timeouts in settings are minutes rather than seconds.
    """
    try:
        mode = os.stat(KVM).st_mode
    except OSError:
        return "tcg"
    if stat.S_ISCHR(mode) and os.access(KVM, os.R_OK | os.W_OK):
        return "kvm"
    return "tcg"


def _tcl_word(word: str) -> str:
    """Quote one word for the generated script's ``spawn`` line.

    The script is Tcl and ``spawn`` takes its arguments without a shell, so a
    path that is not obviously safe gets Tcl's own quoting rather than the
    shell's.
    """
    if word and not set(word) & set(" \t\n{}[]$\\\"';"):
        return word
    return '"' + re.sub(r'([\\"$\[\]])', r"\\\1", word) + '"'


def _tail(text: str, lines: int) -> list[str]:
    return text.splitlines()[-lines:]


class Harness:
    """One VM-test session: host-side setup, then the tests themselves.

    Stateful because the firmware, the accelerator and the disk images are
    found once and used by every guest; the alternative is threading five
    arguments through every call.
    """

    def __init__(self, layout: Layout, runner: Runner, console: Console) -> None:
        self.layout = layout
        self.runner = runner
        self.console = console
        self.ovmf_code = ""
        self.ovmf_vars = ""
        self.accel = _acceleration()
        self.root_image = layout.vm / "root.img"
        self.data_images = tuple(
            layout.vm / f"data{number}.img" for number in range(1, DATA_DISK_COUNT + 1)
        )
        self._tests: dict[str, Callable[[], bool]] = {
            "live-boot": self._test_live_boot,
            "refuse-blank": self._test_refuse_blank,
            "dry-run": self._test_dry_run,
            "install": self._test_install,
        }

    # -- host-side setup ---------------------------------------------------

    def prepare(self) -> None:
        """Everything that has to be true before the first guest boots."""
        if not self.layout.iso.is_file():
            raise BuildError(f"no ISO at {self.layout.iso}.\nRun `ubuntu-uki-iso build iso` first.")
        Runner.require(QEMU, "expect")
        self._find_ovmf()
        self._make_disks()

    def _find_ovmf(self) -> None:
        for candidate in OVMF_CANDIDATES:
            if "secboot" in candidate:
                continue
            if Path(candidate).is_file():
                self.ovmf_code = candidate
                break

        if not self.ovmf_code:
            looked = "\n  ".join(c for c in OVMF_CANDIDATES if "secboot" not in c)
            raise BuildError(
                "no OVMF firmware found; the VM tests need it to boot the ISO the way "
                f"the machine does.\nLooked for:\n  {looked}\nInstall the `ovmf` package."
            )

        # Only the 4M firmware has a matching NVRAM template; without one QEMU
        # is given the code image alone, and efibootmgr's writes go nowhere
        # that outlives the run.
        if self.ovmf_code.endswith("OVMF_CODE_4M.fd") and Path(OVMF_VARS_4M).is_file():
            self.ovmf_vars = OVMF_VARS_4M

    def _make_disks(self) -> None:
        """Create the images that have not been created yet.

        ``truncate`` makes them sparse, so the sizes are about what the guest
        sees rather than what the disk holds: a 2 GB disk behaves exactly
        like an 8 TB one for everything the tests check.
        """
        self.layout.vm.mkdir(parents=True, exist_ok=True)
        self.layout.vm_logs.mkdir(parents=True, exist_ok=True)

        sizes = (
            (self.root_image, VM_ROOT_DISK_SIZE),
            *((image, VM_DATA_DISK_SIZE) for image in self.data_images),
        )
        for path, size in sizes:
            if not path.exists():
                self.runner.run("truncate", "-s", size, path, check=False)

    def _reset_disks(self) -> None:
        """Throw the images away and start from blank ones.

        A test that begins from whatever the last one left behind proves
        nothing about what happens on a blank machine, and sparse files are
        cheap enough to be worth the certainty.
        """
        self.runner.run("rm", "-f", self.root_image, *self.data_images, check=False)
        self._make_disks()

    def report(self) -> None:
        """What this run is about to do, before it spends fifteen minutes doing it."""
        self.console.step("ubuntu-uki-iso VM tests")
        self.console.info(f"iso:        {self.layout.iso}")
        self.console.info(f"ovmf code:  {self.ovmf_code}")
        self.console.info(f"ovmf vars:  {self.ovmf_vars or '(none; NVRAM will not persist)'}")
        note = "  (no KVM — boots are slow, this is expected)" if self.accel == "tcg" else ""
        self.console.info(f"accel:      {self.accel}{note}")
        self.console.info(
            f"disks:      {VM_ROOT_DISK_SIZE} root (nvme) + "
            f"{DATA_DISK_COUNT}x {VM_DATA_DISK_SIZE} (ahci)"
        )

    # -- running one guest -------------------------------------------------

    def _qemu_argv(self, name: str) -> list[str]:
        """The QEMU command line for one guest.

        The root disk is NVMe and the data disks are AHCI on purpose: those are
        the two driver paths the real machine takes, so the installer is driven
        down the same code it will run on the machine.

        The ISO is attached as a **USB stick**, not as a CD-ROM, because that is
        how it is deployed — and the difference is not cosmetic. A CD-ROM goes
        through El Torito, which firmware reads from the boot catalog. A USB
        stick goes through the partition table: the firmware finds the ESP on
        the ISO's GPT and runs the UKI from it. Those are different code paths
        in the firmware and only the second one is used in practice.

        It also means this harness is the only thing that ever *executes* the
        hybrid layout. ``build/iso.py`` parses the finished GPT and refuses an
        image without an EFI System Partition, but parsing proves the bytes are
        there and not that firmware will boot them. Until this ran, that
        distinction was untested.
        """
        argv = [
            QEMU,
            "-machine",
            f"q35,accel={self.accel}",
            "-m",
            str(VM_MEMORY_MB),
            "-smp",
            str(VM_CPUS),
            "-display",
            "none",
            "-serial",
            "stdio",
            "-no-reboot",
        ]

        if self.ovmf_vars:
            nvram = self.layout.vm / f"{name}-VARS.fd"
            argv += [
                "-drive",
                f"if=pflash,format=raw,unit=0,readonly=on,file={self.ovmf_code}",
                "-drive",
                f"if=pflash,format=raw,unit=1,file={nvram}",
            ]
        else:
            argv += ["-bios", self.ovmf_code]

        argv += self._disk_args()
        argv += self._usb_stick_args()

        # The root disk is blank and carries no bootloader, so with no boot
        # order to prefer, firmware falls to the USB stick. That is the same
        # thing it does on the real machine when the ESP is the only bootable
        # thing it can find.
        argv += ["-boot", "menu=off,order=c"]
        return argv

    def _usb_stick_args(self) -> list[str]:
        """The installation medium, as a USB mass storage device.

        This is the medium the live environment will *see*, and it consumes a
        ``/dev/sd*`` letter like any other disk — which is why the tests
        discover the data disks instead of naming them. See
        :meth:`_data_disks_shell`.
        """
        return [
            "-device",
            "qemu-xhci,id=xhci",
            "-drive",
            f"file={self.layout.iso},if=none,id=usbstick,format=raw,readonly=on",
            "-device",
            "usb-storage,drive=usbstick,id=stick",
        ]

    def _disk_args(self) -> list[str]:
        """The drives, in the order the guest's device names come out of.

        One controller carries every data disk, which is what makes them
        ``/dev/sda`` upward in the order they are listed. The tests name those
        devices explicitly, so the order is load-bearing.

        AHCI rather than something more capacious, because on the real machine
        the eight data disks are on AHCI too — and exercising the controller
        the array actually lives on is worth more than exercising the eighth
        disk. QEMU's emulated ICH9 AHCI has ``AHCI_PORTS`` ports, which is why
        :data:`VM_DATA_DISKS` is what it is; the module-level check refuses a
        count that would not fit rather than letting QEMU fail with
        "Bus 'ahci.6' not found", which names nothing useful.

        The root disk stays NVMe emulation, because that one is fully faithful:
        the machine's root disk really is NVMe, and ``BLK_DEV_NVME`` gets
        exercised on its own path rather than as a second SCSI disk.
        """
        args = [
            "-drive",
            f"file={self.root_image},if=none,id=rootdisk,format=raw,cache=unsafe",
            "-device",
            "nvme,serial=ROOTDISK,drive=rootdisk",
            "-device",
            "ahci,id=ahci",
        ]
        for index, image in enumerate(self.data_images):
            args += [
                "-drive",
                f"file={image},if=none,id=d{index},format=raw,cache=unsafe",
                "-device",
                f"ide-hd,drive=d{index},bus=ahci.{index},serial=DATA{index}",
            ]
        return args

    def _expect_script(self, name: str, command: str) -> str:
        """The expect program that boots one guest and runs one command.

        It waits for the login prompt, logs in, waits for a shell prompt, sends
        the command and a carriage return as two separate sends, waits for the
        sentinel, and powers off. Nothing between those points is parsed: the
        guest's output is left in the log for the assertions to search, so a
        test that never reaches the sentinel is a boot that did not get that
        far rather than a prompt that was misread.
        """
        spawn = " ".join(_tcl_word(word) for word in self._qemu_argv(name))
        typed = f"{command}\necho {SENTINEL_TYPED}"

        lines = [
            f"set timeout {VM_BOOT_TIMEOUT}",
            "log_user 1",
            f"spawn {spawn}",
            "expect {",
            '  -re {login: } { send -- "root\\r"; exp_continue }',
            "  -re {[#$] $} { }",
            '  timeout { puts "\\n===UBUNTU_UKI_ISO-TIMEOUT-BOOT==="; exit 42 }',
            '  eof { puts "\\n===UBUNTU_UKI_ISO-EOF-BOOT==="; exit 43 }',
            "}",
            f"set timeout {VM_COMMAND_TIMEOUT}",
            # Braces keep Tcl from expanding $ and [] inside the guest command;
            # the carriage return is a separate send for the same reason.
            f"send -- {{{typed}}}",
            'send -- "\\r"',
            "expect {",
            f'  -re {{{SENTINEL}}} {{ puts "\\n{REACHED}" }}',
            '  timeout { puts "\\n===UBUNTU_UKI_ISO-TIMEOUT-CMD==="; exit 44 }',
            '  eof { puts "\\n===UBUNTU_UKI_ISO-EOF-CMD==="; exit 45 }',
            "}",
            'send -- "poweroff\\r"',
            "expect eof",
            "exit 0",
            "",
        ]
        return "\n".join(lines)

    def _guest_run(self, name: str, command: str) -> bool:
        """Boot, run one command, power off. True if the command finished.

        The outer ``timeout`` is the backstop for a guest that hangs before the
        sentinel; the script's own timeouts are per phase, and a guest that
        stalls outside both would otherwise hold the run open indefinitely.
        """
        log = self.layout.vm_logs / f"{name}.log"
        script = self.layout.vm / f"{name}.exp"

        if self.ovmf_vars:
            # Per-run NVRAM, so efibootmgr writes are contained to the test
            # that made them instead of leaking into the next guest's boot
            # entries.
            shutil.copyfile(self.ovmf_vars, self.layout.vm / f"{name}-VARS.fd")

        script.write_text(self._expect_script(name, command), encoding="utf-8")

        self.console.info(f"guest: {name}  (accel={self.accel}, log {self._shown(log)})")
        log.unlink(missing_ok=True)

        result = self.runner.run(
            "timeout",
            str(VM_BOOT_TIMEOUT + VM_COMMAND_TIMEOUT),
            "expect",
            "-f",
            script,
            check=False,
            log=log,
        )

        text = self._log_text(name)
        if REACHED not in text:
            self.console.error(
                f"{name}: the guest never reached the sentinel (expect rc={result.returncode})"
            )
            self.console.info(f"last lines of {self._shown(log)}:")
            for line in _tail(text, TAIL_LINES):
                self.console.grey(line)
            return False

        self.console.ok(f"{name} reached the shell and ran the command")
        return True

    # -- reading the logs --------------------------------------------------

    def _log_text(self, name: str) -> str:
        path = self.layout.vm_logs / f"{name}.log"
        if not path.is_file():
            return ""
        # The console emits CRs and the guest's output is not always valid
        # UTF-8. Neither is content, and neither should stop a test being read
        # — a log that cannot be decoded is exactly when it is wanted.
        text = path.read_text(encoding="utf-8", errors="replace")
        return text.replace("\r\n", "\n").replace("\r", "")

    def _show_evidence(self, text: str, pattern: str | None) -> None:
        if pattern is None:
            return
        for match in re.findall(pattern, text)[-1:]:
            self.console.grey(match)

    def _assert(self, name: str, checks: Sequence[Check]) -> bool:
        text = self._log_text(name)
        passed = True
        for check in checks:
            if check.regex:
                occurrences = len(re.findall(check.needle, text))
            else:
                occurrences = text.count(check.needle)
            found = occurrences >= check.minimum

            if found:
                self.console.ok(check.ok)
            elif check.required:
                self.console.error(check.fail)
                if occurrences:
                    self.console.error(f"  expected at least {check.minimum}, found {occurrences}")
                self._show_evidence(text, check.evidence)
                passed = False
        return passed

    def _shown(self, path: Path) -> str:
        """A path as the operator would type it, when it is inside the workspace."""
        try:
            return str(path.relative_to(self.layout.root))
        except ValueError:
            return str(path)

    # -- guest-side fixtures -----------------------------------------------

    def _data_disks_shell(self, var: str = "DATA") -> str:
        """Shell that discovers the data disks from inside the guest.

        Names are discovered rather than hardcoded because the installation
        medium is itself a ``/dev/sd*`` device — it is attached over USB, so it
        consumes a letter like any other disk — and which letter it takes
        depends on the order the controllers are enumerated.

        That order is probably stable, and "probably stable" is exactly the
        kind of assumption that produces a test failing for reasons that look
        like a bug in the installer. Asking the guest what it has removes the
        question: the data disks are every ``sd*`` disk except the one carrying
        the ISO's volume label.

        The NVMe root disk never matches ``/dev/sd?``, so it needs no
        excluding.
        """
        return "\n".join(
            [
                f'{var}=""',
                f"LIVE=$(blkid -L {VOLID} 2>/dev/null)",
                "for d in /dev/sd?; do",
                '  [ "$d" = "$LIVE" ] && continue',
                f'  {var}="${var} $d"',
                "done",
            ]
        )

    def _installer(self, *leading: str) -> str:
        """The installer as it exists inside the live image.

        One place, because the command moved from a script in the ISO to a
        console script on PATH and will move again.

        ``UBUNTU_UKI_ISO_EXPECTED_MD_DEVICES`` is set here rather than left at its
        default. The harness emulates a smaller array than the machine has, and
        the installer verifies the member count it finds against what it
        expects — so the two have to agree. Passing it also means the setting
        is exercised: a value plumbed through correctly is the only kind that
        can be wrong here.
        """
        return "\n".join(
            (
                self._data_disks_shell(),
                " ".join(
                    (
                        f"UBUNTU_UKI_ISO_EXPECTED_MD_DEVICES={VM_DATA_DISKS}",
                        "ubuntu-uki-iso install",
                        *leading,
                        "--root-disk",
                        ROOT_DEVICE,
                        "--data-disks",
                        '"$DATA"',
                        "--raid=reuse",
                    )
                ),
            )
        )

    def _fake_array(self) -> str:
        """Build a populated array in the guest, then take it down again.

        This is the state the real machine is in, and the reuse path is only
        reachable from it: with blank disks the installer refuses, which is
        what the blank-disk test is for. The array is built and filled from
        inside the guest, through the emulated devices, because building it on
        the host would mean the loop devices this harness exists to avoid.
        """
        return "\n".join(
            [
                self._data_disks_shell(),
                'echo "data disks:$DATA"',
                f"mdadm --create {MD_DEVICE} --level={MDADM_LEVEL} "
                f"--raid-devices={VM_DATA_DISKS} --chunk={EXPECTED_MD_CHUNK} "
                f"--metadata={RAID_METADATA_VERSION} --uuid={EXPECTED_MD_UUID} "
                f"--run $DATA",
                f"mkfs.ext4 -q -L fakearray {MD_DEVICE}",
                f"mkdir -p {FAKE_MOUNT}; mount {MD_DEVICE} {FAKE_MOUNT}",
                f"echo precious-data > {FAKE_MOUNT}/canary.txt; sync; umount {FAKE_MOUNT}",
                f"mdadm --stop {MD_DEVICE}",
            ]
        )

    # -- the tests ---------------------------------------------------------

    def run(self, name: str) -> bool:
        self.console.rule(f"test: {name}")
        return self._tests[name]()

    def _test_live_boot(self) -> bool:
        name = "live-boot"
        # The block-device listing rides along here because this is the first
        # guest that boots, and every later test addresses the data disks by
        # name. Checking the names here means a controller or ordering surprise
        # is reported as "these are the devices the guest actually has" instead
        # of surfacing later as an installer refusal that looks like a bug in
        # the safety logic.
        command = "\n".join(
            [
                "echo BEGIN",
                "findmnt -n -o FSTYPE /",
                "cat /proc/cmdline",
                "ls -l /payload/rootfs.squashfs",
                "ls -l /payload/debs/*.deb",
                "ls -l /live/filesystem.squashfs 2>/dev/null",
                self._data_disks_shell(),
                'set -- $DATA; echo "data-disks:$#"',
                "lsblk -dn -o NAME,SIZE,TRAN",
                "echo END",
            ]
        )
        if not self._guest_run(name, command):
            return False

        return self._assert(
            name,
            [
                Check("/ is an overlay", "/ is not an overlay", "overlay", regex=True),
                Check(
                    "the live cmdline reached userspace",
                    "/proc/cmdline does not carry rd.live.dir=/live",
                    "rd.live.dir=/live",
                ),
                Check(
                    "the payload is visible on the medium",
                    "the payload squashfs is not on the mounted medium",
                    "rootfs.squashfs",
                ),
                # The payload carries no kernel, so these are what the target's
                # kernel comes from. An ISO missing them installs a root
                # filesystem that cannot boot, and the installer is the only
                # thing that would notice.
                Check(
                    "the kernel packages are on the medium",
                    "no kernel packages on the medium — the target would get no kernel",
                    r"linux-image-\S+\.deb",
                    regex=True,
                ),
                # That the ISO booted at all is proved by reaching this point.
                # How it booted is worth asserting separately: a USB transport
                # means firmware read the ISO's GPT and found the ESP on it,
                # whereas a CD-ROM would have gone through El Torito and proved
                # nothing about the hybrid layout. Those are different code
                # paths, and only one of them is used in practice.
                Check(
                    "the live medium arrived over USB",
                    "the medium is not on a USB transport — this run did not "
                    "exercise the path the machine actually boots from",
                    r"usb\s*$",
                    regex=True,
                ),
                Check(
                    "the guest sees the root disk as NVMe",
                    "the guest has no /dev/nvme0n1",
                    "nvme0n1",
                ),
                # A count rather than names: the installation medium is itself
                # an sd* disk, so which letters the data disks get depends on
                # enumeration order. See _data_disks_shell.
                Check(
                    f"the guest found exactly {VM_DATA_DISKS} data disks",
                    f"the guest did not find {VM_DATA_DISKS} data disks — the "
                    "installer is about to be pointed at the wrong set",
                    f"data-disks:{VM_DATA_DISKS}",
                ),
            ],
        )

    def _test_refuse_blank(self) -> bool:
        name = "refuse-blank"
        self._reset_disks()

        # --apply, not --dry-run, and that is the whole design of this test.
        #
        # Under --dry-run nothing is written whatever the installer decides, so
        # DISKS-UNCHANGED would hold even if the refusal never fired. Running
        # with --apply makes the refusal the *only* thing standing between the
        # guest's disks and a partition table — so if the blank-disk check ever
        # regresses, this test writes to the disks and says so.
        #
        # --assume-yes skips the typed confirmation, which the installer
        # requires before any destructive command. It is safe to skip precisely
        # because the run should never reach one.
        #
        # The checksums are taken inside the guest, so the disks are proved
        # untouched rather than assumed to be.
        command = "\n".join(
            [
                "echo BEGIN",
                self._data_disks_shell(),
                "md5sum $DATA > /tmp/before.md5",
                self._installer("--apply --assume-yes"),
                'echo "RC=$?"',
                "md5sum $DATA > /tmp/after.md5",
                "if diff -q /tmp/before.md5 /tmp/after.md5 >/dev/null; "
                "then echo DISKS-UNCHANGED; else echo DISKS-CHANGED; "
                "diff /tmp/before.md5 /tmp/after.md5; fi",
                "echo END",
            ]
        )
        if not self._guest_run(name, command):
            return False

        return self._assert(
            name,
            [
                Check("the installer refused", "no refusal was printed", "REFUSING"),
                Check(
                    "exited non-zero (2)",
                    "expected exit code 2 on refusal",
                    "RC=2",
                    evidence=r"RC=[0-9]+",
                ),
                Check(
                    "said so explicitly",
                    "did not state that nothing had been changed",
                    "Nothing has been changed",
                ),
                Check(
                    "every data disk byte-identical after the refusal",
                    "a disk changed during a run that refused to do anything",
                    "DISKS-UNCHANGED",
                ),
            ],
        )

    def _test_dry_run(self) -> bool:
        name = "dry-run"
        self._reset_disks()

        setup = "\n".join(
            [
                "echo BEGIN",
                self._fake_array(),
                self._data_disks_shell(),
                "md5sum $DATA > /tmp/before.md5",
                "cat /tmp/before.md5",
                "echo END",
            ]
        )
        if not self._guest_run(f"{name}-setup", setup):
            return False

        command = "\n".join(
            [
                "echo BEGIN",
                self._installer("--dry-run"),
                'echo "RC=$?"',
                "md5sum $DATA > /tmp/after.md5",
                "if diff -q /tmp/before.md5 /tmp/after.md5 >/dev/null; "
                "then echo ARRAY-BYTES-UNCHANGED; else echo ARRAY-BYTES-CHANGED; "
                "diff /tmp/before.md5 /tmp/after.md5; fi",
                "echo END",
            ]
        )
        if not self._guest_run(name, command):
            return False

        return self._assert(
            name,
            [
                Check("ran in dry-run mode", "did not run in dry-run mode", "DRY-RUN"),
                Check(
                    "reported that nothing was changed",
                    "did not report that nothing changed",
                    "Nothing was changed",
                ),
                Check(
                    "every data disk byte-identical after the run",
                    "the data disks were modified by a dry run",
                    "ARRAY-BYTES-UNCHANGED",
                ),
                # The check above passes just as well for a preflight that
                # never looked at the disks, so this asserts the opposite: that
                # each one was examined and recognised.
                #
                # Twenty times over, because "md member" appears once per disk
                # in the scan report — a run that examined one disk and gave up
                # would not reach the count.
                #
                # Note what is deliberately NOT asserted: that the array was
                # assembled. Assembling is a mutation, so a dry run does not do
                # it, and a test demanding it would be demanding that dry runs
                # mutate.
                Check(
                    "every data disk was examined and found to be an md member",
                    "the disks were never examined",
                    "md member",
                    minimum=DATA_DISK_COUNT,
                ),
            ],
        )

    def _test_install(self) -> bool:
        name = "install"
        self._reset_disks()

        # The array comes first: --raid=reuse refuses blank disks by design,
        # so an install run with nothing on them would be testing the refusal
        # again rather than the install.
        command = "\n".join(
            [
                "echo BEGIN",
                self._fake_array(),
                self._installer("--apply", "--assume-yes"),
                'echo "RC=$?"',
                "ls -l /mnt/ubuntu-uki-iso-target/boot/efi/EFI/Linux/ 2>/dev/null",
                "cat /mnt/ubuntu-uki-iso-target/etc/kernel/cmdline",
                # The kernel is not in the payload: it is installed from the
                # medium with apt, so whether it landed is a separate question
                # from whether the installer returned 0. Prefixed so that the
                # check below cannot be satisfied by the installer's own chatter
                # echoing the release back.
                "ls /mnt/ubuntu-uki-iso-target/lib/modules/ | sed 's/^/modules:/'",
                # The hook that makes the kernel package install above build a
                # UKI at all. Checked separately from the UKI itself, so that a
                # missing trigger is reported as a missing trigger rather than
                # as "no UKI appeared".
                "ls /mnt/ubuntu-uki-iso-target/etc/kernel/postinst.d/ 2>/dev/null",
                "test -d /mnt/ubuntu-uki-iso-target/var/tmp/kernel-debs"
                " && echo LEFTOVER || echo deb-staging-cleaned",
                "efibootmgr | grep -i ubuntu-uki-iso",
                "echo END",
            ]
        )
        if not self._guest_run(name, command):
            return False

        return self._assert(
            name,
            [
                Check("installer exited 0", "installer did not exit 0", "RC=0"),
                Check(
                    "the kernel package installed",
                    "the target has no /lib/modules/<release> — the kernel was not "
                    "installed, so the UKI has no modules to load",
                    r"modules:\S*-ubuntu-uki-iso",
                    regex=True,
                ),
                Check(
                    "the kernel-install trigger is installed",
                    "no postinst hook in the target: installing a kernel package "
                    "there would build no UKI, now or on any future upgrade",
                    "zz-ubuntu-uki-iso",
                ),
                Check(
                    "the staged packages were cleaned up",
                    "the packages are still in the target's /var/tmp",
                    "deb-staging-cleaned",
                ),
                Check(
                    "the target's cmdline names its own root filesystem",
                    "the cmdline still carries a placeholder: the UKI built from it "
                    "would panic at boot looking for a filesystem that does not exist",
                    r"root=UUID=[0-9a-f]{8}-",
                    regex=True,
                ),
                Check(
                    "a UKI landed on the ESP",
                    "no UKI on the ESP",
                    r"EFI/Linux/.*\.efi",
                    regex=True,
                ),
                Check("firmware entry created", "no firmware entry", "ubuntu-uki-iso"),
                # The fallback loader is only replaced by --bless, after a
                # successful boot. Reported, never failed: the shell installer
                # printed this line on its dry-run path only, so its absence
                # here proves nothing.
                Check(
                    "\\EFI\\BOOT\\BOOTX64.EFI was left alone",
                    "the fallback loader was replaced",
                    "BOOTX64.EFI would be left untouched",
                    required=False,
                ),
            ],
        )

    def summary(self, selected: Sequence[str], failed: Sequence[str]) -> None:
        self.console.rule()
        if failed:
            self.console.error(f"FAILED: {', '.join(failed)}")
        else:
            self.console.info(f"all {len(selected)} tests passed")
        self.console.info(f"logs: {self._shown(self.layout.vm_logs)}")


def run_tests(
    which: str = "all",
    layout: Layout | None = None,
    runner: Runner | None = None,
    console: Console | None = None,
) -> int:
    """Run the QEMU tests for the built ISO. 0 if they all passed, 1 otherwise.

    A missing ISO, a missing tool or a missing firmware raises rather than
    returning 1: those are questions about the machine, not test results, and
    the caller has a better place to say so.
    """
    console = get_console() if console is None else console
    layout = Layout() if layout is None else layout

    if which == "all":
        selected: tuple[str, ...] = TEST_NAMES
    elif which in TEST_NAMES:
        selected = (which,)
    else:
        raise ConfigError(
            f"unknown VM test {which!r}: expected one of {', '.join(('all', *TEST_NAMES))}"
        )

    if runner is None:
        # A harness that only prints what it would boot is not a harness. The
        # caller's runner is taken as it stands — an explicit dry run is a
        # choice — but the default actually runs.
        runner = Runner(dry_run=False, console=console)
    elif runner.dry_run:
        console.warn(
            "this runner is in dry-run mode: no guest will be booted and every test will fail"
        )

    harness = Harness(layout=layout, runner=runner, console=console)
    harness.prepare()
    harness.report()

    failed = [name for name in selected if not harness.run(name)]
    harness.summary(selected, failed)
    return 1 if failed else 0
