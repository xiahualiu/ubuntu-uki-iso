# ubuntu-uki-iso

A reproducible, purpose-built Ubuntu 26.04 image for one specific machine: a custom kernel trimmed to the hardware actually present, delivered as a UKI the UEFI firmware boots directly — no GRUB, no boot manager, no shim.

> **Status: implemented, partially verified.** Everything in the repository layout below exists and runs. What has been proven by execution, and what has only been written, is set out honestly in [Verification](#verification) — the short version is that the static half of the build (config, containers, ISO assembly, package resolution) is verified, and the long half (the 45-minute kernel build, the rootfs, the UKI, and the QEMU tests) has not been run here.
>
> The design below carries one revision since it was written: the kernel now reaches the target as a Debian package rather than being copied in, which is recorded under [Implementation notes](#implementation-notes) along with the decisions implementation forced. Of the five open items the design listed, four are answered — one of them by discovering that the design's `xorriso` flags did not work — and one, the dracut `rd.live.*` incantation, still needs a built initramfs to settle.
>
> **A note on the language.** The design notes assume shell scripts. This is a Python package instead: a `pyproject.toml`, a console script, and subpackages for the build, the installer and the kernel-install plugins. Two consequences worth knowing before reading further: `python3-minimal` is now a dependency of the installed system, because the plugins that run on every kernel upgrade are Python; and the Makefile is gone, because its targets were always thin wrappers and the interesting part — which flag reaches which step, and what a failure means — was spread across both it and the scripts.

---

## Why

The machine (`xiahualab`) currently runs stock Ubuntu 26.04.1 with a GRUB/shim boot chain, a single-NVMe root, an 8-disk RAID0 data array, and no swap at all. The goal is to replace that ad-hoc state with something reproducible.

Three things make this worth doing as a project rather than a one-off script:

1. **The boot chain becomes auditable.** A UKI is one PE binary containing kernel, dracut initramfs, and kernel command line. What the firmware jumps to is exactly what you built, with nothing in between to drift.
2. **The image is reproducible from source.** Today the box's state lives in `curtin`-era fstab entries and hand-installed packages. After this, `ubuntu-uki-iso build kernel && ubuntu-uki-iso build iso` regenerates the whole thing, and CI proves it still builds.
3. **The 58 TB array is treated as precious.** The installer's default path assembles and mounts the existing array without ever writing to it. Creating a RAID0 is opt-in, gated, and preflight-checked.

**Deliverable:** a project that builds a custom kernel, a UKI, and a hybrid ISO which live-boots via dracut and installs a minimal server + Docker rootfs onto this exact storage layout. The kernel reaches the target as a Debian package built here and carried on the medium, so a later kernel upgrade on that machine is the same operation the install was.

---

## Target hardware

The image is built for this machine and no other. That specificity is the point.

| Component | Detail | Kernel driver |
|---|---|---|
| CPU | AMD Ryzen 9 5900XT (Zen 3, 16c/32t) | `KVM_AMD`, `AMD_NB`, `SENSORS_K10TEMP` |
| RAM | 121 GiB | — |
| GPU | NVIDIA RTX 2080 (TU104, Turing) | `DRM_NOUVEAU=m` — rootfs only |
| NIC | Intel I226-V | `igc` |
| Root disk | Samsung 980 PRO 1 TB NVMe | `BLK_DEV_NVME` |
| Data disks | 8× Seagate ST8000DM004 8 TB SATA | `SATA_AHCI`, `MD` + `MD_RAID0` |
| Chipset | AMD Matisse/Vermeer (X570-class) | `I2C_PIIX4`, `AMD_NB` |

---

## Locked decisions

| Area | Decision |
|---|---|
| Kernel | Ubuntu `linux-source-7.0.0` (7.0.0-31.31, matches running kernel), **conservative driver trim** |
| Boot | **No bootloader.** Firmware boots the UKI by EFI path; cmdline embedded in the UKI |
| Swap | **None at all** — no swapfile, no swap partition, no zram. Hibernation is impossible by construction |
| GPU | Nouveau in the **rootfs only**; explicitly excluded from every initramfs |
| Rootfs | **Minimal server + Docker** — no GNOME, no snapd, no display server |
| ISO | **From scratch** — mmdebstrap → squashfs → xorriso hybrid, UEFI boot |
| RAID | **Auto-detect and reuse** the existing `md0` by UUID; create only if explicitly flagged |
| CI | **Self-hosted runner** on this box |

---

## Verified environment facts

Checked against the live system. These drive the design.

- **systemd 259** with `/usr/lib/kernel/install.d/90-uki-copy.install` and `50-dracut.install` present → the native `kernel-install` UKI path exists and dracut is already wired into it.
- **`systemd-boot-efi` is NOT installed** → `/usr/lib/systemd/boot/efi/linuxx64.efi.stub` is absent, so UKI generation needs that package. It provides the stub *without* installing the bootloader — exactly what a no-bootloader design wants.
- **dracut 110** is the active initramfs generator (`/etc/kernel/postinst.d/dracut`) and `/etc/dracut.conf.d/` is **empty** — a clean slate, no distro defaults to fight.
- Modules confirmed present: `dmsquash-live`, `mdraid`, `squash-squashfs`, `overlayfs`, `dm`. This is what makes a dracut-based live ISO viable. `livenet` is absent, which is fine.
- **Docker 29.8 works unprivileged** (user in `docker` group, overlayfs, buildx). Since **passwordless sudo is NOT available**, every build step runs in a container — this is a hard design constraint, not a preference.
- ESP is **1 GB with 6.4 MB used**. The firewall is UKI count: ~50 MB each means roughly 10–15 fit, so retention must be deliberate.
- Secure Boot is **disabled**, so an unsigned UKI boots. Enabling it later would require enrolling keys — noted as a future concern, not handled here.
- `mdadm` v4.5, RAID0 metadata 1.2, UUID `3955b9de:69b7772b:e7cd882f:92edf554`, 512K chunks, whole-disk members with no partitions.
- Build tooling available in apt: `mmdebstrap` 1.5.7, `xorriso` 1.5.6, `mtools`, `dosfstools`, `squashfs-tools` 4.7.5, `sbsigntool` 0.9.4, plus kernel build deps (`flex`, `bison`, `libssl-dev`, `libelf-dev`, `libncurses-dev`, `dwarves`/`pahole`, `zstd`).

---

## Architecture

### Boot model — UKI straight from firmware

```
UEFI firmware ──> \EFI\Linux\<machine-id>-<ver>.efi   (one file: kernel + initramfs + cmdline)
                  \EFI\BOOT\BOOTX64.EFI               (byte-identical copy = NVRAM-loss safety net)
```

Configuration is declarative and lives in the rootfs:

```ini
# /etc/kernel/install.conf
layout=uki
initrd_generator=dracut
uki_generator=ukify
```

```
# /etc/kernel/cmdline
root=UUID=<root-uuid> ro quiet
```

`kernel-install add <ver> /boot/vmlinuz-<ver>` then drives dracut → ukify → `90-uki-copy.install`, landing the UKI on the ESP. What calls `kernel-install` is the image's own hook in `/etc/kernel/postinst.d/`, because nothing in a stock Ubuntu system does — see [Implementation notes](#implementation-notes). The firmware entry is created once:

```bash
efibootmgr --create --disk /dev/nvme0n1 --part 1 \
  --label "xiahualab" --loader '\EFI\Linux\<machine-id>-<ver>.efi'
```

Two consequences of having no bootloader that the design absorbs:

- **The cmdline cannot be edited at boot.** Changing it means rebuilding the UKI. That is the trade for having no boot manager, and it is why `data/cmdline/installed` is a versioned file in the repo rather than something edited in place on the box.
- **There is no menu to fall back to.** `\EFI\BOOT\BOOTX64.EFI` is updated only *after* a new UKI has booted successfully, so a bad kernel can never become the only option.

### Kernel — conservative driver trim

Source is `linux-source-7.0.0` from apt (same version as the running kernel, so the config base is exact). The trim starts from `/boot/config-7.0.0-31-generic` and disables drivers for hardware that does not exist.

**Disabled:** WLAN, Bluetooth, sound, most SCSI/HBA drivers, legacy IDE, PCMCIA, ISDN, hamradio, non-Intel Ethernet, and every DRM driver except nouveau.

**Kept, deliberately:** `ahci`, `nvme`, `md` + `MD_RAID0`, `ext4`, `vfat`, `igc`, `DRM_NOUVEAU=m`, `USB_XHCI_HCD`, `HID`, `KVM_AMD`, `EFI_STUB`, and the whole container substrate.

**The trap:** this kind of trim breaks Docker long after the build succeeds, because the symptom (`docker run` fails) appears far from the cause (a `=n` in a config fragment). These must survive untouched — treat as a **never-disable list**:

```
CONFIG_NAMESPACES, NET_NS, PID_NS, IPC_NS, UTS_NS, USER_NS
CONFIG_CGROUPS, MEMCG, CGROUP_BPF, CGROUP_PIDS, CGROUP_FREEZER
CONFIG_NETFILTER, NF_CONNTRACK, NF_NAT, NF_TABLES, NFT_*
CONFIG_NETFILTER_XT_MATCH_*, BRIDGE, BRIDGE_NETFILTER, VETH, VXLAN
CONFIG_OVERLAY_FS, KEYS, SECCOMP, SECCOMP_FILTER, POSIX_MQUEUE, BPF_SYSCALL
```

Build via `make bindeb-pkg -j32` with `CONFIG_LOCALVERSION="-ubuntu-uki-iso"`, producing `linux-image-*.deb` and `linux-headers-*.deb`. The headers matter: Docker and any future DKMS module needs them matching.

### ISO — mmdebstrap → squashfs → xorriso, booting the same UKI

```
mmdebstrap (resolute, minbase) ─┬─ live rootfs ──> install the kernel .deb
        │                       │       └─> kernel-install ──> the ISO's own UKI ──> efi.img ─┐
        │                       │                                                             │
        │                       └─ installed rootfs ──> mksquashfs ──> /payload/rootfs.squashfs│
        │                                        (deliberately no kernel)                    │
        └─ make bindeb-pkg ──> kernel .debs ──> /payload/debs/*.deb ──────────────────────────┤
                                                                                              │
                                                      xorriso hybrid ─────────────────────────┴─> ubuntu_uki_iso.iso
```

Two rootfs images rather than one. The design has a single image "stripped of live-only state" at install time; building both up front keeps the installer a copy operation and keeps the destructive step simple. See [Implementation notes](#implementation-notes).

The ISO carries the **same kernel** as the installed system — the same `.deb`, built once. In the live image it is installed at build time, because the ISO's own UKI is built from it. In the installed image it is not installed at all: the payload travels without a kernel, and the target gets one by installing that package from the medium, with apt. That is not a shortcut — it is the point. The install becomes the first kernel upgrade the machine has, through the same path as every later one. See [Implementation notes](#implementation-notes).

The UKIs differ in one respect: the live image must point at the squashfs and the installed image must point at its root filesystem.

Live boot is dracut's, not Debian's `live-boot`. The layout is Debian-conventional (`/live/filesystem.squashfs`), so the cmdline must point dracut at it explicitly:

```
root=live:CDLABEL=UBUNTU-UKI-ISO rd.live.image rd.live.dir=/live
rd.live.squashimg=filesystem.squashfs rd.live.overlay.overlayfs=1
rd.md=0 rd.luks=0 rd.lvm=0 console=tty0 console=ttyS0,115200n8
```

> dracut's own default is `/LiveOS/squashfs.img`, so the `rd.live.dir` and `rd.live.squashimg` overrides are load-bearing, not decorative. This remains the least certain part of the design and the one open item — confirm against the built initramfs before changing the ISO.
>
> `rd.md=0` is deliberate: dracut does not auto-assemble the data array at live boot, so the array stays untouched until the installer's preflight deliberately assembles it.

xorriso produces a UEFI-bootable hybrid: an `efi.img` FAT image holding the UKI at `/EFI/BOOT/BOOTX64.EFI`, attached as an El Torito entry *and* appended as a GPT partition. The GPT is the one that matters — the ISO is written to a USB stick, where firmware finds the ESP through the partition table rather than through El Torito. The El Torito entry is kept because it costs nothing and leaves CD boot working as a fallback. Note that the combination the design originally named, `-e efi.img -no-emul-boot -isohybrid-gpt-basdat`, does not achieve this — see [the xorriso finding](#the-xorriso-finding). BIOS booting is out of scope — every target machine is UEFI.

### Installer — the RAID safety design

This is the part that can destroy 58 TB, so the default path is the safe one and destruction requires deliberate, typed intent.

```
ubuntu-uki-iso install
  │
  ├─ PREFLIGHT (always, read-only)
  │    blkid + mdadm --examine on every target disk
  │    ├─ matching md0 found  ──> ASSEMBLE + mount RW, never write superblocks  [default]
  │    ├─ no md0, no --raid=create ──> STOP, explain, exit
  │    └─ no md0, --raid=create ──> refuse if ANY filesystem/superblock present
  │
  ├─ --dry-run  ──> print every command, execute nothing        [DEFAULT ON]
  │
  └─ CONFIRM ──> typed phrase, only for destructive steps
```

`--dry-run` is the default rather than an opt-in. The failure mode this guards against is running the installer on the wrong machine — or on this one by accident — and finding out after `mdadm --create`.

**The kernel is installed, not copied.** The installer formats the root filesystem, unsquashes the payload onto it, writes the fstab and the real root UUID into the command line, and then copies the two `.deb`s the medium carries into the target and runs `apt-get install` there, in a chroot. The package's own postinst runs `kernel-install` → dracut → ukify → the UKI on the ESP. Nothing about the UKI is special-cased: the same maintainer script that installs the kernel here is the one that will run on every future kernel upgrade on that machine, so the mechanism is exercised before there is anything to lose. See [Implementation notes](#implementation-notes).

---

## Repository layout

A Python package. The build steps, the installer and the kernel-install plugins are all `ubuntu-uki-iso.*` modules; the configuration is packaged data they read.

```
ubuntu-uki-iso/
├── pyproject.toml              # packaging, console script, ruff/mypy/pyright/pytest config
├── .pre-commit-config.yaml     # the same checks, run on commit
├── build/Dockerfile            # build env — no host sudo needed
├── ubuntu_uki_iso/
│   ├── cli.py                  # the `ubuntu-uki-iso` command
│   ├── proc.py                 # run/probe/destructive — the ONLY path to a mutation
│   ├── log.py                  # the only path to output
│   ├── errors.py               # Refusal vs BuildError — the exception IS the control flow
│   ├── paths.py settings.py    # where things live; what can differ per machine
│   ├── pe.py gpt.py            # read a UKI's sections; read an ISO's partition table
│   ├── shim.py                 # generates the entry points that live on the target
│   ├── config/                 # what the image is made of, and its self-consistency checks
│   │   ├── kernel.py           #   the trim, the guard list, and the checks between them
│   │   ├── boot.py             #   cmdline templates, dracut
│   │   └── packages.py
│   ├── build/                  # producing the artifacts
│   │   ├── container.py        #   docker invocation, and the chown-back
│   │   ├── kernel.py rootfs.py uki.py squashfs.py iso.py
│   │   └── hooks/              #   mmdebstrap --customize-hook, one per rootfs
│   ├── installer/              # writing the result onto a machine
│   │   ├── preflight.py        #   THE RAID GUARDRAILS — read this one
│   │   ├── device.py           #   read-only device inspection
│   │   ├── partition.py raid.py target.py uki.py efientry.py
│   │   └── cli.py context.py
│   ├── ukis/                   #   retention, and the firmware fallback
│   └── data/                   # the configuration itself, packaged
│       ├── kernel/             #   config-trim.fragment, never-disable.list, install.conf
│       ├── dracut/ cmdline/    #   00-live.conf, 10-installed.conf, cmdline templates
│       ├── packages/           #   {live,installed}.list
├── tests/                      # pytest: the decision table, the parsers, the dry run
└── .github/workflows/build.yml # self-hosted runner
```

**Why the configuration stayed as files.** The trim fragment is 300 lines of Kconfig, the package lists and cmdline templates are things a person reads and edits. As Python string literals they would be worse in every way that matters. They live under `ubuntu_uki_iso/data/` so that a wheel carries them, and `paths.data_file()` checks for them at load time — a missing data file means the wheel was built wrong, and finding that out thirty minutes into a kernel build is the expensive way.

**Why the kernel-install plugins are shims.** They have to be executables in `/etc/kernel/install.d/`, on a machine assembled from distribution packages with no pip. So the package tree is copied to `/usr/local/lib/ubuntu-uki-iso` and `shim.py` generates four-line executables that put it on `sys.path` and call in. The logic stays importable and testable; the thing sitting on the upgrade path stays short enough to read in full.

Nothing large is ever committed: `.gitignore` excludes `*.deb`, `*.iso`, `*.squashfs`, `out/`, `linux-source*/`, `.venv/`.

---

## Roadmap

Ordered so each phase proves itself before the next depends on it. The code for every phase below exists; what remains is running them in this order and recording the results in the verification table above. Phase 1 is the right place to start, because it is the only phase whose failure mode is "select the old entry in GRUB".

**Phase 0 — scaffold.** Repo, Dockerfile with `mmdebstrap xorriso systemd-ukify systemd-boot-efi dracut squashfs-tools mtools dosfstools sbsigntool` and kernel build deps.

**Phase 1 — kernel.** Fetch source, apply trim, build. Proven by installing the `.deb` normally onto this box and booting it **through the existing GRUB** — the safest possible first proof, and fully reversible by selecting the old entry.

**Phase 2 — UKI + direct firmware boot.** Add `systemd-boot-efi` + `systemd-ukify`, set `layout=uki`, build the UKI, then create a **new** `efibootmgr` entry for it. The existing GRUB entry is left untouched and remains the default; `\EFI\BOOT\BOOTX64.EFI` is **not** overwritten until the UKI has booted successfully.

**Phase 3 — live rootfs + ISO.** mmdebstrap rootfs, squashfs, ISO's own UKI, xorriso. Proven by `qemu-system-x86_64 -bios OVMF.fd -cdrom out.iso` reaching a shell with the squashfs mounted.

**Phase 4 — installer.** Preflight, dry-run, RAID reuse path, partitioning, rootfs copy, UKI install, `efibootmgr`. Proven in QEMU against **virtual disks only** — file-backed, presented to the guest as `/dev/nvme0n1` and `/dev/sd[a-h]`, so the host never creates a block device at all.

**Phase 5 — CI.** Self-hosted runner, `workflow_dispatch` for kernel builds, tag-triggered ISO release with artifacts attached to GitHub Releases.

---

## Verification

Two tables, because "the code exists" and "the code has been run" are different claims and this project can destroy 58 TB if the difference is blurred.

### Proven by execution

Everything here was run against this machine or the build container.

| Claim | How it was proven | Result |
|---|---|---|
| The trim applies to the real config | `ubuntu-uki-iso doctor` — the actual `config-7.0.0-31-generic`, merged with the fragment, resolved with `olddefconfig` | 2023 config symbols differ from base |
| What the trim removed is what was meant | The same run writes `out/kernel/config.diff`, a per-symbol before/after list | 2025 decisions, readable |
| The guards survive the trim | Same run: every symbol in `never-disable.list` checked against the merged `.config` | 41/41 present |
| Nothing on the guard list is trimmed | `ubuntu-uki-iso verify`, plus a negative control that adds `# CONFIG_SCSI_LOWLEVEL is not set` | clean; negative control caught it, naming the symbol |
| The trim fragment names real symbols | `ubuntu-uki-iso doctor` reports fragment symbols absent from the base config | 2 found and fixed (`CONFIG_SCSI_DC395x` has a lowercase x; reiserfs no longer exists) |
| The kernel release is what the design expects | `make -s kernelrelease` on the configured source tree | `7.0.14-ubuntu-uki-iso` |
| The build image works | `docker build --target build`, including an assertion that `linuxx64.efi.stub` is present | built; stub present |
| Every package name resolves | `apt-get install --dry-run` for both lists against resolute | 79/79 |
| mmdebstrap works under the container's privileges | `minbase` bootstrap plus a `--customize-hook` that chroots and writes into the target | succeeded in 18 s |
| The ISO is a UEFI hybrid | xorriso 1.5.6, then parsing the finished ISO's GPT and El Torito records | ESP present; the design's original flag set correctly rejected |
| QEMU accepts the harness's device topology | Ran the exact generated argv in the test image and read OVMF's output | NVMe root + 2 AHCI data disks + a USB stick, all enumerated by firmware |
| The firmware actually looks at the USB stick | Same run — OVMF's boot menu output | `Boot0005 "UEFI QEMU QEMU USB HARDDRIVE"`, attempted and correctly not found (the ISO was a stub) |
| The guard list covers what nothing else would catch | `pytest` asserts the USB, SCSI-disk and storage symbols are all guarded, and that the obsolete virtio ones are not | passes |
| The SCSI removal is safe | Configured the real kernel both ways and diffed: everything the machine and the live medium need survives `SCSI_LOWLEVEL=n` | `BLK_DEV_SR`, `BLK_DEV_SD`, `SATA_AHCI`, `BLK_DEV_NVME`, `MD_RAID0`, `USB_STORAGE` all survive |
| The code is sound | `ruff check`, `ruff format --check`, `pyright`, `mypy` — or `pre-commit run --all-files`, which runs all four | clean |
| The RAID decision table is correct | `pytest` — constructed device state, every device helper stubbed | 15 cases |
| The whole suite | `ubuntu-uki-iso test unit` | 123 passed, 1 skipped |
| A UKI's command line can be read back | PE section parsing, against hand-built PE files | 8 cases |
| The kernel packages resolve offline | `pytest` — the build-time guard, against a stubbed chroot: it accepts a resolvable install, and fails the build on one apt would have to fetch for | 3 cases |
| The UKI is harvested from where kernel-install puts it | `pytest` — `EFI/Linux/*.efi`, against a built staging tree, including that the fallback copy is not mistaken for it | 5 cases |
| A kernel package install builds a UKI | `pytest` — the postinst hook against a stubbed `kernel-install`: the verb, the arguments `run-parts` passes, the boot root, and that a failure is passed through rather than swallowed | 6 cases |
| Nothing on the target calls kernel-install by itself | The maintainer scripts `builddeb` generates (read from the unpacked source tree), and `/etc/kernel/postinst.d` on the build host | true — which is why `ukis/trigger.py` exists |

The negative control in row 4 is the one worth reading. Re-adding the line that caused the SCSI_VIRTIO bug now refuses the build, and names the symbol and the line in `never-disable.list` that requires it. Before the fix, that line produced a kernel that built cleanly.

### Not yet run

Written and wired up, but never executed. These need the kernel build, which takes roughly 45 minutes and has not been run here.

| Claim | How it is proven | Status |
|---|---|---|
| Kernel boots | `.deb` installed, booted via existing GRUB, `uname -r` shows `-ubuntu-uki-iso` | not run |
| Trim did not break Docker | `docker run --rm hello-world`, plus `docker network create` (exercises netfilter) | not run |
| Trim did not break storage | `mdadm --assemble`, mount the array, read a file | not run |
| UKI boots with no bootloader | Reboot, select the `ubuntu-uki-iso` entry in firmware menu, `/proc/cmdline` matches `/etc/kernel/cmdline` | not run |
| dracut's `rd.live.*` finds the squashfs | Boot the ISO; the live cmdline is the one thing the design flagged as least certain | not run |
| ISO live-boots | QEMU + OVMF to a shell; `findmnt /` shows the overlay, and the guest sees the nine disks the tests address | assertion written in `ubuntu_uki_iso/qemu/harness.py` |
| There is no swap | `swapon --show` is empty and `zramctl` lists nothing | not run |
| Installer is non-destructive | QEMU with a populated fake array; the eight disks are checksummed in-guest before and after | assertion written |
| Installer refuses blanks by default | QEMU with empty disks, `--apply`, expecting a refusal, exit 2, and disks unchanged | assertion written — runs with `--apply` deliberately, so the refusal is the only thing preventing a write |
| The kernel installs from the medium with apt | QEMU install test: `/lib/modules/<release>` appears in the target, the staged `.deb`s are cleaned up again, the target's cmdline carries a real UUID, the postinst trigger is installed, and the UKI on the ESP came from the package's postinst rather than from the installer | assertion written |
| A kernel upgrade on the installed machine rebuilds the UKI | Install a second, newer pair of `.deb`s on the target and watch `kernel-install` produce a new UKI, the fallback copy follow it, and retention keep the old one | not run — and this is the half of the design that the package delivery exists for |
| CI reproduces | Workflow run on the self-hosted runner produces a byte-comparable ISO | **see below** |

On the last row: a byte-comparable ISO is not currently achievable and the claim should be weakened rather than quietly kept. `mksquashfs -all-time` and `xorriso -volume_date` pin the timestamps that are under this repository's control, but the kernel build embeds a build ID and its own timestamps, so two kernel builds produce different `.deb`s and everything downstream inherits that. The workflow proves the ISO *builds* reproducibly from a given kernel; it does not prove the kernel is reproducible. Making that true is a separate piece of work.

---

## Implementation notes

Decisions the design did not cover, or where implementing it changed the shape of the thing.

**Two rootfs images, not one.** The design has a single rootfs that gets "stripped of live-only state" at install time. There are two images instead — `live` (the installer's operating system) and `installed` (minimal server + Docker) — because stripping during install makes the destructive step also the complicated one, and because `data/packages/{live,installed}.list` only means something if both are built. The installer stays a copy operation. The installed image travels on the ISO as the installer's *payload*; the two words describe one artifact from two directions, and `ubuntu_uki_iso/build/rootfs.py` says so.

**The kernel reaches the target as a package.** The payload rootfs is built without a kernel. The medium carries the `linux-image` and `linux-headers` `.deb`s that `make bindeb-pkg` produces, and the installer installs them with `apt-get` inside a chroot on the target. The reason is the one the whole project is about: an install that copies a kernel in, and an upgrade that installs one, are two different mechanisms, and only the second is ever exercised again. Doing it this way means the machine's first kernel upgrade is the one that installed it.

Two consequences are accepted deliberately. The install stops being a copy operation — it is a package install that builds an initramfs, so minutes rather than seconds. And apt has to resolve the package's dependencies from the payload alone, with no package lists and no network, which is a real risk: a missing dependency would surface on the target, after partitioning, with the disk already formatted. So it is checked at build time instead, by `verify_packages_resolve` in `build/hooks/installed.py`, which simulates the install inside the built rootfs and fails the build if apt would have to fetch anything. The QEMU install test is what proves the real thing behaves the same way.

**Nothing on a stock Ubuntu system calls `kernel-install`.** The kernel packages `make bindeb-pkg` produces have maintainer scripts that do exactly one thing: `run-parts` over `/etc/kernel/postinst.d` and `/etc/kernel/postrm.d`. They do not call `kernel-install`, and neither does anything else on the machine — the package that would, `systemd-boot`, is deliberately not installed, because this design has no bootloader. Checked rather than assumed: on the build host `/etc/kernel/postinst.d` holds dracut, kdump-tools, unattended-upgrades, update-notifier, xx-update-initrd-links, zz-shim and zz-update-grub, and not one of them mentions `kernel-install`.

So the installed image carries its own: `/etc/kernel/postinst.d/zz-ubuntu-uki-iso` calls `kernel-install add` with the release and the image path `run-parts` hands it, and the matching hook in `postrm.d` calls `kernel-install remove`. That is what makes the `install.d` plugins run at all — they are a chain, not alternatives. The boot root is set to `settings.ESP_MOUNT` rather than left to `kernel-install`'s autodetection, which on a machine with more than one ESP is a guess, and it is the guess that decides whether the machine boots.

Without this the failure would have been silent: the kernel package installs, dracut leaves a working initramfs in `/boot`, and no UKI is ever built — so the firmware goes on booting whatever UKI was there before, which on a fresh install is nothing.

**The reuse path mounts read-only, with `norecovery`.** The design says "ASSEMBLE + mount RW, never write superblocks". Mounting RW is itself a write: ext4 updates the superblock's mount count and last-mounted time, and a plain `mount -o ro` still replays the journal. The verification mount is therefore `-o ro,norecovery`, which genuinely touches nothing. The installed system mounts the array read-write at boot, as normal.

**QEMU-relevant drivers are kept.** The trim disables non-Intel Ethernet and everything under DRM except nouveau, but keeps `VIRTIO_BLK`, `VIRTIO_NET`, `SCSI_VIRTIO`, `E1000`/`E1000E`, `ATA_PIIX` and `ATA_GENERIC`. Without them the ISO cannot be tested in a VM, and an untestable image is the thing this project exists to avoid. They are small and the comments say why.

**The installed command line is a template.** `data/cmdline/installed` carries `root=UUID=@@ROOT_UUID@@` because the real UUID does not exist until the installer has partitioned the disk. `ubuntu_uki_iso/installer/target.py` substitutes it and then *verifies* no `@@` survives, and `ubuntu_uki_iso/installer/uki.py` reads the value back out of the finished UKI's `.cmdline` section. A UKI carrying the placeholder builds fine and panics at boot; that is worth three checks.

**`--bless` is gated on evidence.** The design says `\EFI\BOOT\BOOTX64.EFI` is updated only after the new UKI has booted successfully, but does not say how that is known. A systemd unit in the installed image writes the running kernel release to `/var/lib/xiahualab/booted`, and `--bless` refuses unless that file names the kernel currently running. The original loader is copied to `BOOTX64.EFI.pre-ubuntu-uki-iso` exactly once, so a second run cannot destroy the only known-good copy.

**VM tests use QEMU file-backed disks, not host loop devices.** The design says "loop-backed disks, never real ones". Files are used instead, presented to the guest through QEMU's NVMe and AHCI emulation, so the guest sees `/dev/nvme0n1` and `/dev/sd[a-h]` exactly as the real machine does — while the host never creates a block device the installer could be pointed at by mistake. Disk images live under `out/vm/` and are the only storage any test can reach.

**The build container is not `--privileged`.** It gets `SYS_ADMIN` and unconfined seccomp, because mmdebstrap, chroot and mount need them, and nothing else. It mounts the workspace and nothing else — in particular not `/dev`, so it cannot see `/dev/sd{a..h}`. A bug in a build script has no path to the array.

**Two symbols in the design's notes are not real.** `CONFIG_EFIVARFS` is not a Kconfig symbol in 7.0 (efivarfs is built unconditionally with `CONFIG_EFI`) and neither is `CONFIG_DM_LINEAR` (dm-linear is part of `BLK_DEV_DM`). Both are omitted from `never-disable.list` deliberately: listing a symbol that does not exist makes the guard either always fail or always pass for the wrong reason. `ubuntu-uki-iso doctor` reports fragment symbols that the base config has never heard of, which is how the two dead lines in the trim were found.

**A menu gate removed a driver nothing was guarding.** `CONFIG_SCSI_LOWLEVEL` is a `menuconfig` symbol, so a single line disabling it removes everything inside its block — including `CONFIG_SCSI_VIRTIO`, which it never names. At the time the VM harness presented its data disks over virtio-scsi, so this was a kernel that would build, install and boot perfectly, and whose test suite would report a machine with no disks. A comment three lines away in the trim fragment claimed the driver was being kept.

The harness no longer uses virtio-scsi, so that particular symbol is now deliberately gone. The lesson outlived it: the guard list is no longer framed as "Docker-critical" but as "anything this project needs, for any reason", and it now covers the USB stack the live medium arrives on as well as the storage stack.

Two things came out of it. The guard list is no longer framed as "Docker-critical" but as "anything this project needs, for any reason", and it now covers the storage and virtio stack; re-adding the offending line refuses the build and names the symbol. And the fragment carries a note where the line was, because the mistake is an inviting one to repeat.

**The VM has two data disks, not eight, and they are on AHCI.** QEMU's emulated `ahci` is the ICH9 controller with six ports, so the seventh disk is rejected outright::

    qemu-system-x86_64: -device ide-hd,drive=d6,bus=ahci.6: Bus 'ahci.6' not found

Eight do not fit on one controller, and two controllers make the guest's device names depend on PCI probe order — a test that fails depending on enumeration order is worse than one that fails outright, because it looks like a bug in the installer.

Count is the cheap thing to give up. The decision table's combinatorial cases — partial membership, split arrays, wrong member counts — are covered exhaustively by the pytest suite against constructed device state. What the VM adds is proof that the plumbing works on a real kernel: that an array assembles, that the reuse path leaves the bytes alone, that the refusal fires. Two disks prove all three, and AHCI is the controller the real array is on.

**The VM boots the ISO from a USB stick, not a CD-ROM.** This is not cosmetic. A CD-ROM goes through El Torito, which firmware reads from the boot catalog; a USB stick goes through the partition table, where firmware has to find the ESP on the ISO's GPT. Those are different code paths, and only the second is used in practice.

It also means the harness is the only thing that ever *executes* the hybrid layout. `build/iso.py` parses the finished GPT and refuses an image with no EFI System Partition, but parsing proves the bytes are present, not that firmware will boot them.

Attaching the medium over USB has a consequence worth knowing: it is itself a `/dev/sd*` device, so it takes a letter. The tests therefore discover the data disks at runtime — every `sd*` disk except the one carrying the ISO's volume label — rather than naming them.

**The SCSI low-level layer is disabled, and nothing needs it.** `CONFIG_SCSI_LOWLEVEL` is a `menuconfig` gate covering the host bus adapter drivers, and once the harness stopped using virtio-scsi nothing in it was used: the data disks are on libata/AHCI, the root disk is NVMe, and the live medium arrives over USB mass storage — none of which is inside that block. What the block *does* contain is `CONFIG_SCSI_VIRTIO`, whose silent removal by this gate is the bug described above. The difference now is that the removal is deliberate, and the fragment lists what was checked to survive it.

**Debug info is kept, with an opt-out.** The base config has `CONFIG_DEBUG_INFO=y` and `CONFIG_DEBUG_INFO_BTF=y`, which is why `dwarves`/`pahole` is in the build image. Keeping them is the conservative choice and costs roughly 45 minutes and 15 GB of scratch. `ubuntu-uki-iso build kernel `--no-debug-info` merges `data/kernel/config-nodebug.fragment` for a much faster iteration build; the kernel is functionally identical, it just cannot symbolize its own stack traces.

## Building

```bash
python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'
.venv/bin/pre-commit install             # once — then every commit is checked

ubuntu-uki-iso verify                    # seconds, offline — the guard checks
ubuntu-uki-iso doctor                    # ~2 min — validates the trim before spending 45 on it
ubuntu-uki-iso build kernel              # ~45 min; leaves the .debs in out/kernel/
ubuntu-uki-iso build kernel --no-debug-info   # a fraction of that, for iterating
ubuntu-uki-iso build all                 # rootfs, UKI, squashfs, xorriso
ubuntu-uki-iso test unit                 # pytest
ubuntu-uki-iso test vm                   # QEMU, opt-in and slow (this host has no KVM)
```

`ubuntu-uki-iso --help` lists the rest: `clean`, `fix-perms`, and `install` — the last of which runs on the target machine, not here.

Every build step runs in a container; nothing needs sudo on the host, and the container is not `--privileged` and never sees `/dev`. `ubuntu-uki-iso clean` removes `out/` even if a killed build left it root-owned — which it can, because the host user has no sudo to fix that with.

## Risks

- **The RAID array.** Mitigated by dry-run-by-default, preflight scanning, typed confirmation, and testing exclusively against virtual disks. The real `/dev/sd{a..h}` are never a test target. The build container is not `--privileged` and does not mount `/dev`, so no build script can reach the array either — the protection is not only procedural.
- **Docker silently broken by the kernel trim.** Mitigated by the never-disable list and an explicit `docker network create` test, which exercises netfilter rather than just the daemon. The same list covers anything else the project depends on — see the SCSI_VIRTIO note under implementation notes, which is what that framing came from.
- **Python on the boot path.** The kernel-install plugins and the postinst hook that triggers them are Python, so `python3-minimal` is a dependency of the installed system. That puts an interpreter on the kernel-upgrade path — and, since the hook runs *during* the package install, on the package-install path too: a broken interpreter fails the kernel install rather than quietly leaving the UKI unbuilt. That is the deliberate direction (the alternative is a machine that believes it has a new kernel and boots the old one), but it does mean an interpreter problem becomes a dpkg problem. The plugins themselves are written to exit 0 on anything they do not understand rather than failing a kernel install, and the fallback copy is verified by size after writing.
- **dracut live-boot cmdline.** The `rd.live.*` keys are the least certain part of this design.
- **ESP exhaustion.** 1 GB holds ~10–15 UKIs, so retention is not optional. Implemented as a kernel-install plugin (`ubuntu_uki_iso/ukis/retention.py`) that runs after `90-uki-copy.install`, keeps 3, prunes oldest, and refuses to remove either the UKI of the running kernel or the one just installed. The "just installed" protection is the one that matters, because this runs at exactly the moment when the running kernel is the *old* one and the new UKI is the one that must survive.
- **The kernel install needs no network, and has to keep not needing it.** The payload carries no kernel, so `apt-get install` on the target is the step that could reach for the archive. It is checked at build time by simulating the install inside the built payload, and the build fails if apt would have to fetch anything — but the simulation is apt in a container, not apt on the target. The QEMU install test is what closes that gap; until it has run, the check is a strong argument rather than a proof.
- **Unsigned UKI + future Secure Boot.** Booting is fine today (SB disabled). Enabling it later requires enrolling keys; `sbsign` is in the build image so the UKI can be signed when needed, but no key ceremony is designed here.
- **Self-hosted runner trust.** A runner on this box executes repo workflows with access to the RAID0 data. Keep the repo private, or restrict which workflows may run.

---

## Open items

These were the things worth confirming before spending 45 minutes on a kernel build. Four are now answered, and answering them changed the implementation.

- [x] **`systemd-ukify` / `systemd-boot-efi` package names and the stub path.** Both are real
      packages at `259.5-0ubuntu3.4`, and `systemd-boot-efi` installs
      `/usr/lib/systemd/boot/efi/linuxx64.efi.stub`. The build image asserts the stub exists
      and fails at build time if it ever stops being true.
- [x] **`systemd-zram-generator` is the package name on resolute** — confirmed at 1.2.1-2, then dropped when the design stopped using zram.
- [ ] **The precise `rd.live.*` incantation for `/live/filesystem.squashfs` under dracut 110.**
      Still unconfirmed — this needs a built initramfs to check against, so it stays open until
      the first `ubuntu-uki-iso build iso`. The values in `data/cmdline/live` are the design's, and the
      design's own note applies: dracut defaults to `/LiveOS/squashfs.img`, so the
      `rd.live.dir` and `rd.live.squashimg` overrides are load-bearing.
- [x] **Whether `make bindeb-pkg` needs more than the standard dep set.** It does not — but the
      base config references Canonical's module-signing PKI via `CONFIG_SYSTEM_TRUSTED_KEYS`,
      which a standalone build cannot reach. The trim fragment empties that and
      `CONFIG_SYSTEM_REVOCATION_KEYS`; without it the build fails *after* the expensive compile.
- [x] **The `xorriso` flag combination for a UEFI-only hybrid.** The design's combination —
      `-e efi.img -no-emul-boot -isohybrid-gpt-basdat` — is **not sufficient**, and the failure
      is silent. See below.

### The xorriso finding

Tested against xorriso 1.5.6 on the build image. `-isohybrid-gpt-basdat` on its own produces **no MBR and no GPT at all**: the resulting ISO has a valid El Torito UEFI entry, boots fine from a CD/DVD or `qemu -cdrom`, and is completely invisible to firmware when written to a USB stick. Nothing about the build reports a problem.

What actually makes the image hybrid is appending the EFI image as a partition:

```
-append_partition 2 0xef <host-path-to-efi.img>   # note: HOST path
-appended_part_as_gpt
```

`-e boot/efi.img` takes a path *inside* the ISO and `-append_partition` takes one on the *build host*. Both point at the same file and they are not interchangeable.

`ubuntu_uki_iso/build/iso.py` now parses the finished ISO's GPT and fails the build unless it finds a partition typed `C12A7328-F81F-11D2-BA4B-00A0C93EC93B` (EFI System Partition), in addition to checking the El Torito entry and the volume label. Verified in both directions: the corrected command yields an ESP, and the original combination is caught and rejected.
