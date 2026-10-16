"""
Kernel configuration validation helpers for tests.
"""

from __future__ import annotations

import re
from pathlib import Path

from ubuntu_uki_iso.errors import BuildError, ConfigError
from ubuntu_uki_iso.kernel.config import ConfigEntry, TristateValue
from ubuntu_uki_iso.kernel.config import config_override as get_config_override

_CONFIG_SET_RE = re.compile(r"^(CONFIG_[A-Za-z0-9_]+)=(.*)$")
_CONFIG_UNSET_RE = re.compile(r"^# (CONFIG_[A-Za-z0-9_]+) is not set$")


def guard_entries() -> list[tuple[str, str | None]]:
    return [
        # None permits y or m; y requires built-in; m permits built-in or module.
        # Test requirements for boot storage, live media and container support.
        # --- Namespaces: the isolation runc builds every container out of -----------
        ("CONFIG_NAMESPACES", "y"),
        ("CONFIG_NET_NS", "y"),
        ("CONFIG_PID_NS", "y"),
        ("CONFIG_IPC_NS", "y"),
        ("CONFIG_UTS_NS", "y"),
        ("CONFIG_USER_NS", "y"),
        # --- Control groups: resource limits, and the freezer docker stop uses ------
        ("CONFIG_CGROUPS", "y"),
        ("CONFIG_MEMCG", "y"),
        ("CONFIG_CGROUP_BPF", "y"),
        ("CONFIG_CGROUP_PIDS", "y"),
        ("CONFIG_CGROUP_FREEZER", "y"),
        # --- Netfilter: docker's port publishing and network create ----------------
        # `docker run --rm hello-world` does not exercise these. `docker network
        # create` does. That is why the verification table tests both.
        ("CONFIG_NETFILTER", "y"),
        ("CONFIG_NF_CONNTRACK", None),
        ("CONFIG_NF_NAT", None),
        ("CONFIG_NF_TABLES", None),
        ("CONFIG_NETFILTER_XT_MATCH_ADDRTYPE", None),
        ("CONFIG_NETFILTER_XT_MATCH_CONNTRACK", None),
        ("CONFIG_NETFILTER_XT_MATCH_IPVS", None),
        # --- Bridge and overlay networking for the default bridge driver -----------
        ("CONFIG_BRIDGE", None),
        ("CONFIG_BRIDGE_NETFILTER", None),
        ("CONFIG_VETH", None),
        ("CONFIG_VXLAN", None),
        # --- The container filesystem and the syscall surface ----------------------
        # OVERLAY_FS is what the overlayfs storage driver needs, and it is also what
        # dmsquash-live uses for the live ISO's writable layer.
        ("CONFIG_OVERLAY_FS", None),
        ("CONFIG_KEYS", "y"),
        ("CONFIG_SECCOMP", "y"),
        ("CONFIG_SECCOMP_FILTER", "y"),
        ("CONFIG_POSIX_MQUEUE", "y"),
        ("CONFIG_BPF_SYSCALL", "y"),
        # --- Detected desktop hardware ---------------------------------------------
        ("CONFIG_IGC", None),
        ("CONFIG_DRM_SIMPLEDRM", "y"),
        ("CONFIG_SOUND", "y"),
        ("CONFIG_SND_HDA_INTEL", None),
        ("CONFIG_USB4", None),
        ("CONFIG_TYPEC_UCSI", None),
        ("CONFIG_UCSI_CCG", None),
        ("CONFIG_I2C_NVIDIA_GPU", None),
        ("CONFIG_SENSORS_K10TEMP", None),
        ("CONFIG_X86_AMD_PSTATE", "y"),
        ("CONFIG_USB_HID", None),
        # --- Storage this machine actually uses ------------------------------------
        # Guarded for the same reason as everything else: the trim disables a great
        # many drivers, and "the disk driver" is not a thing anyone expects to lose.
        ("CONFIG_SCSI", "y"),
        ("CONFIG_BLK_DEV_SD", "y"),
        ("CONFIG_SATA_AHCI", None),
        ("CONFIG_BLK_DEV_NVME", None),
        ("CONFIG_MD", "y"),
        ("CONFIG_BLK_DEV_MD", "y"),
        ("CONFIG_MD_RAID0", None),
        ("CONFIG_EXT4_FS", "y"),
        ("CONFIG_VFAT_FS", "y"),
        # --- What the live medium arrives on, and what the array is on -------------
        # Not Docker, but exactly the same failure shape: disabling one of these builds
        # a perfectly good kernel that then cannot boot its own installation medium, or
        # whose test suite reports a machine with no disks.
        #
        # This section exists because of a real one. CONFIG_SCSI_VIRTIO lived inside
        # the `if SCSI_LOWLEVEL` menu block in drivers/scsi/Kconfig, so a single line
        # disabling SCSI_LOWLEVEL removed it — without naming it, and while a comment a
        # few lines away claimed it was being kept. Nothing guarded it, because this
        # list was framed as "Docker-critical" and virtio-scsi is not a Docker concern.
        # The only symptom would have been a test suite reporting no disks.
        #
        # The harness no longer uses virtio-scsi — its data disks are on AHCI, like the
        # machine's — so those entries are gone. What replaced them is the stack the
        # live ISO actually boots through: the ISO is written to a USB stick, so the
        # media is read by usb-storage into the SCSI disk driver, not by a CD-ROM
        # driver off an ATAPI bus.
        # USB mass storage, and the controllers underneath it. Without these the live
        # environment cannot see the stick it booted from, which dracut discovers late
        # and reports as a rescue shell.
        ("CONFIG_USB_STORAGE", None),
        ("CONFIG_USB_XHCI_HCD", None),
        ("CONFIG_USB_EHCI_HCD", None),
        # The USB stick is a SCSI disk as far as the kernel is concerned.
        ("CONFIG_SCSI", "y"),
        ("CONFIG_BLK_DEV_SD", "y"),
        # The live medium's filesystem, and the overlay dracut puts on top of it.
        ("CONFIG_ISO9660_FS", None),
        ("CONFIG_SQUASHFS", "y"),
        ("CONFIG_BLK_DEV_LOOP", None),
        # SATA and NVMe: the eight data disks and the root disk.
        ("CONFIG_ATA", "y"),
        ("CONFIG_SATA_AHCI", None),
        ("CONFIG_BLK_DEV_NVME", None),
    ]


def parse_config_values(text: str) -> dict[str, str | None]:
    """
    Kernel ``.config`` text to ``{symbol: value}``.

    Unset symbols map to ``None`` so the difference between "disabled" and
    "not mentioned" stays visible.
    """
    values: dict[str, str | None] = {}
    for line in text.splitlines():
        if match := _CONFIG_SET_RE.match(line):
            values[match.group(1)] = match.group(2).strip().strip('"')
        elif match := _CONFIG_UNSET_RE.match(line):
            values[match.group(1)] = None
    return values


def config_override_symbols(config_override: list[ConfigEntry]) -> set[str]:
    return {entry.symbol for entry in config_override}


def disabled_symbols(config_override: list[ConfigEntry]) -> set[str]:
    return {entry.symbol for entry in config_override if entry.value is TristateValue.N}


def check_guard(
    merged_config_text: str, entries: list[tuple[str, str | None]] | None = None
) -> list[str]:
    entries = entries if entries is not None else guard_entries()
    values = parse_config_values(merged_config_text)
    return [
        f"{symbol} is {values.get(symbol) or 'not set'} but requires "
        f"{symbol}{'=' + requirement if requirement else ''} "
        "(kernel.config.guard_entries)"
        for symbol, requirement in entries
        if not (
            values.get(symbol) == "y" if requirement == "y" else values.get(symbol) in ("y", "m")
        )
    ]


def unknown_config_override_symbols(
    base_config_text: str, config_override: list[ConfigEntry]
) -> set[str]:
    return config_override_symbols(config_override) - parse_config_values(base_config_text).keys()


def guard_overlap(
    guard: list[tuple[str, str | None]] | None = None,
    config_override: list[ConfigEntry] | None = None,
) -> list[str]:
    guard = guard if guard is not None else guard_entries()
    disabled = disabled_symbols(
        config_override if config_override is not None else get_config_override()
    )
    return [symbol for symbol, _ in guard if symbol in disabled]


def _symbol_changes(base: str, merged: str) -> list[tuple[str, str, str]]:
    """
    Per-symbol differences between two kernel configs.

    A symbol-level diff rather than a textual one, for two reasons.

    The practical one: a sorted textual diff of a .config is unreadable, and
    its output cannot be counted reliably — the counts this project quotes are
    of *decisions*, not of lines that moved.

    The conceptual one: the config override is the intent and the merged config is the
    outcome, and they differ wherever Kconfig ``select`` pulls something back
    in. What is worth knowing is which symbols ended up different, and what
    they were before.
    """
    base_values = parse_config_values(base)
    merged_values = parse_config_values(merged)

    changes = []
    for symbol in sorted(set(base_values) | set(merged_values)):
        before = base_values.get(symbol, "<absent>")
        after = merged_values.get(symbol, "<absent>")
        if before != after:
            changes.append((symbol, _show(before), _show(after)))
    return changes


def _show(value: str | None) -> str:
    return "n" if value is None else value


def verify_guard(config: Path, base_config: Path) -> None:
    """
    The check that protects Docker, run before the compile.
    """
    print("==> verify the never-disable guard")

    merged_text = config.read_text(errors="replace")
    base_text = base_config.read_text(errors="replace")

    # A guard entry naming a symbol the base config has never heard of cannot
    # guard anything. That is almost always a typo, and a typo here silently
    # weakens the protection, so it is fatal rather than a warning.
    known = parse_config_values(base_text)
    entries = guard_entries()
    unknown = [(symbol, requirement) for symbol, requirement in entries if symbol not in known]
    if unknown:
        listing = "\n".join(f"  {symbol} (kernel.config.guard_entries)" for symbol, _ in unknown)
        raise BuildError(
            "these never-disable entries are not symbols in the base config:\n"
            f"{listing}\n"
            "A guard entry that does not exist cannot protect anything. Check the "
            "spelling against kernel/config.py:guard_entries."
        )

    failures = check_guard(merged_text, entries)
    if failures:
        listing = "\n".join(f"  {failure}" for failure in failures)
        raise BuildError(
            "the never-disable guard FAILED — the trim removes something this "
            f"project needs:\n{listing}\n\n"
            "Fix kernel/config.py:config_override. If the symbol is only missing "
            "because a menu gate above it was disabled, note that disabling a "
            "menuconfig symbol removes everything in its block."
        )

    print(f"{len(entries)} symbols checked, all present")


def report_unknown_config_override_symbols(base_config: Path) -> None:
    """
    Mention trim lines that did nothing.

    Not fatal — a symbol can legitimately disappear between kernel versions —
    but the usual cause is a typo, and a typo'd trim line is a trim that
    silently did not happen.
    """
    unknown = unknown_config_override_symbols(
        base_config.read_text(errors="replace"), get_config_override()
    )
    if unknown:
        for symbol in sorted(unknown):
            print(
                f"warning: trim config override mentions {symbol}, which is not in the base config — it did nothing"
            )
    else:
        print("every trim symbol exists in the base config")


def validate_config_override(config_override: list[ConfigEntry]) -> None:
    symbols = [entry.symbol for entry in config_override]
    if len(symbols) != len(set(symbols)):
        raise ConfigError("duplicate symbols in config override")
    if any(not re.fullmatch(r"CONFIG_[A-Za-z0-9_]+", symbol) for symbol in symbols):
        raise ConfigError("invalid kernel symbol in config override")


def validate_release(release: str, localversion: str) -> None:
    """
    Check that a built kernel release includes the configured suffix.
    """
    if not release.endswith(localversion):
        raise BuildError(
            f"the kernel release is {release!r}, which does not end in {localversion!r}"
        )


def workstation_requirements() -> dict[str, tuple[str, ...]]:
    """
    Kernel interfaces for Rust, Wine, and Docker; see docs/kernel-workstation.md.
    """
    return {
        "Steam and Proton": (
            "CONFIG_IA32_EMULATION",
            "CONFIG_COMPAT",
            "CONFIG_NTSYNC",
            "CONFIG_FUTEX",
            "CONFIG_EVENTFD",
            "CONFIG_DRM",
            "CONFIG_DRM_KMS_HELPER",
            "CONFIG_SYNC_FILE",
            "CONFIG_DMA_SHARED_BUFFER",
            "CONFIG_INPUT_EVDEV",
            "CONFIG_INPUT_JOYDEV",
            "CONFIG_INPUT_UINPUT",
            "CONFIG_HIDRAW",
            "CONFIG_UHID",
            "CONFIG_HID_STEAM",
            "CONFIG_HID_SONY",
            "CONFIG_HID_PLAYSTATION",
            "CONFIG_HID_NINTENDO",
            "CONFIG_HID_LOGITECH_HIDPP",
            "CONFIG_HID_UNIVERSAL_PIDFF",
            "CONFIG_JOYSTICK_XPAD",
            "CONFIG_JOYSTICK_XPAD_FF",
            "CONFIG_JOYSTICK_XPAD_LEDS",
            "CONFIG_SND_PCM",
            "CONFIG_SND_USB_AUDIO",
        ),
        "Native kernel build and external modules": (
            "CONFIG_BINFMT_ELF",
            "CONFIG_BINFMT_SCRIPT",
            "CONFIG_FUTEX",
            "CONFIG_UNIX98_PTYS",
            "CONFIG_PROC_FS",
            "CONFIG_SYSFS",
            "CONFIG_TMPFS",
            "CONFIG_FILE_LOCKING",
            "CONFIG_MODULES",
            "CONFIG_MODULE_UNLOAD",
            "CONFIG_MODVERSIONS",
        ),
        "Rust development and debugging": (
            "CONFIG_BINFMT_ELF",
            "CONFIG_BINFMT_SCRIPT",
            "CONFIG_BINFMT_MISC",
            "CONFIG_UNIX",
            "CONFIG_INET",
            "CONFIG_FUTEX",
            "CONFIG_FUTEX_PI",
            "CONFIG_EPOLL",
            "CONFIG_EVENTFD",
            "CONFIG_SIGNALFD",
            "CONFIG_TIMERFD",
            "CONFIG_INOTIFY_USER",
            "CONFIG_AIO",
            "CONFIG_IO_URING",
            "CONFIG_UNIX98_PTYS",
            "CONFIG_DEVTMPFS",
            "CONFIG_PROC_FS",
            "CONFIG_SYSFS",
            "CONFIG_TMPFS",
            "CONFIG_FHANDLE",
            "CONFIG_FILE_LOCKING",
            "CONFIG_PERF_EVENTS",
            "CONFIG_KALLSYMS",
            "CONFIG_FUSE_FS",
        ),
        "Wine and desktop peripherals": (
            "CONFIG_IA32_EMULATION",
            "CONFIG_COMPAT",
            "CONFIG_NTSYNC",
            "CONFIG_INPUT_EVDEV",
            "CONFIG_INPUT_JOYDEV",
            "CONFIG_INPUT_UINPUT",
            "CONFIG_HIDRAW",
            "CONFIG_UHID",
            "CONFIG_USB_HID",
            "CONFIG_HID_GENERIC",
            "CONFIG_SND_HDA_INTEL",
            "CONFIG_SND_USB_AUDIO",
            "CONFIG_SND_HRTIMER",
            "CONFIG_DRM",
            "CONFIG_DRM_KMS_HELPER",
            "CONFIG_DMA_SHARED_BUFFER",
        ),
        "Docker isolation and storage": (
            "CONFIG_NAMESPACES",
            "CONFIG_NET_NS",
            "CONFIG_PID_NS",
            "CONFIG_IPC_NS",
            "CONFIG_UTS_NS",
            "CONFIG_USER_NS",
            "CONFIG_CGROUPS",
            "CONFIG_CGROUP_CPUACCT",
            "CONFIG_CGROUP_DEVICE",
            "CONFIG_CGROUP_FREEZER",
            "CONFIG_CGROUP_SCHED",
            "CONFIG_CPUSETS",
            "CONFIG_MEMCG",
            "CONFIG_CFS_BANDWIDTH",
            "CONFIG_CGROUP_PIDS",
            "CONFIG_CGROUP_BPF",
            "CONFIG_KEYS",
            "CONFIG_POSIX_MQUEUE",
            "CONFIG_SECCOMP",
            "CONFIG_SECCOMP_FILTER",
            "CONFIG_OVERLAY_FS",
            "CONFIG_EXT4_FS",
            "CONFIG_EXT4_FS_POSIX_ACL",
            "CONFIG_EXT4_FS_SECURITY",
            "CONFIG_BLK_CGROUP",
            "CONFIG_BLK_DEV_THROTTLING",
        ),
        "Docker networking": (
            "CONFIG_VETH",
            "CONFIG_BRIDGE",
            "CONFIG_BRIDGE_NETFILTER",
            "CONFIG_BRIDGE_VLAN_FILTERING",
            "CONFIG_VXLAN",
            "CONFIG_IP_NF_FILTER",
            "CONFIG_IP_NF_MANGLE",
            "CONFIG_IP_NF_TARGET_MASQUERADE",
            "CONFIG_IP6_NF_FILTER",
            "CONFIG_IP6_NF_MANGLE",
            "CONFIG_IP6_NF_TARGET_MASQUERADE",
            "CONFIG_NETFILTER_XT_MATCH_ADDRTYPE",
            "CONFIG_NETFILTER_XT_MATCH_CONNTRACK",
            "CONFIG_NETFILTER_XT_MATCH_IPVS",
            "CONFIG_NETFILTER_XT_MARK",
            "CONFIG_IP_NF_RAW",
            "CONFIG_IP_NF_NAT",
            "CONFIG_IP6_NF_RAW",
            "CONFIG_IP6_NF_NAT",
            "CONFIG_NF_NAT",
            "CONFIG_NFT_CT",
            "CONFIG_NFT_FIB_IPV4",
            "CONFIG_NFT_FIB_IPV6",
            "CONFIG_NFT_FIB",
            "CONFIG_NFT_MASQ",
            "CONFIG_NFT_NAT",
            "CONFIG_NF_TABLES",
            "CONFIG_MACVLAN",
            "CONFIG_IPVLAN",
            "CONFIG_DUMMY",
            "CONFIG_IP_VS",
            "CONFIG_IP_VS_NFCT",
            "CONFIG_IP_VS_PROTO_TCP",
            "CONFIG_IP_VS_PROTO_UDP",
            "CONFIG_IP_VS_RR",
        ),
    }
