# Ubuntu UKI LiveUSB

Build a custom Ubuntu kernel, two root filesystems and two UKIs, then assemble a UEFI LiveUSB ISO. Boot the live system to install the server SquashFS and its prebuilt UKI onto a target disk.

```text
ubuntu_uki_iso/
  kernel/     source download, configuration and kernel deb build
  uki/        kernel-deb-to-EFI build and boot parameters
  rootfs/     shared live/installed bootstrap and SquashFS builder
  iso/        hybrid UEFI ISO assembly and verification
  installer/  payload deployment, disk checks and optional RAID handling
  qemu/       live boot and installation checks against virtual disks
```

## Setup

Python 3.11 or newer is required. Builders check dependencies and report missing packages; they do not install host packages.

```bash
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'
source .venv/bin/activate
```

| Binary | Entry point | Result |
|---|---|---|
| `ubuntu-uki-kernel` | `kernel.build:main` | Kernel image and headers debs |
| `ubuntu-uki-build` | `uki.build:main` | UKI EFI and initramfs |
| `ubuntu-uki-rootfs` | `rootfs.build:main` | Rootfs and SquashFS images |
| `ubuntu-uki-image` | `iso.build:main` | Hybrid LiveUSB ISO |
| `ubuntu-uki-install` | `installer.cli:main` | Install the payload on a target disk |

The build binaries require `--temp-dir`. Use the same directory throughout. A rootfs build requires root to preserve file ownership. ISO assembly uses mtools and needs no mounts or root privileges.

`build/Dockerfile` provides build dependencies. For local builds install its kernel dependencies and:

```bash
sudo apt-get install apt dpkg mmdebstrap ubuntu-keyring squashfs-tools kmod systemd \
  dracut systemd-ukify systemd-boot-efi mdadm dmsetup zstd xorriso dosfstools mtools
```

## Build the ISO

```bash
ubuntu-uki-kernel --temp-dir out --jobs 32
sudo "$PWD/.venv/bin/ubuntu-uki-rootfs" --temp-dir out --mode all
ubuntu-uki-build --temp-dir out --mode live
ubuntu-uki-build --temp-dir out --mode installed
ubuntu-uki-image --temp-dir out
```

The rootfs and UKI binaries select the image deb matching `kernel/release`; ambiguous inputs are rejected. Both accept `--kernel-deb` for an explicit input. Rootfs defaults to installed mode, accepts `live`, `installed` or `all`, and compresses each image as part of its build. UKI accepts `live` or `installed`.

The checkout command `ubuntu-uki-iso build all` runs this entire pipeline with `out` as the build directory. `build rootfs all`, `build uki live`, `build uki installed` and `build iso` also run individual stages. Run the full pipeline as root inside the provided build container.

The kernel builder extracts the Ubuntu binary `linux-source-7.0.0` package and runs `make bindeb-pkg`. It records the actual upstream release rather than inferring it from Ubuntu's package series. The fixed hardware profile is explained in [docs/kernel-workstation.md](docs/kernel-workstation.md).

Both rootfs profiles get Ubuntu OS metadata from `base-files` and matching runtime modules copied from the kernel image deb. Kernel maintainer scripts are not run. The live rootfs carries the installer; the installed rootfs carries the server packages. Neither rootfs contains build tools for generating a UKI. Machine IDs and SSH keys are cleared; installed systems generate fresh SSH host keys on first boot.

Dracut uses `hostonly=no` and explicit driver/module lists. UKIs embed build-host OS metadata, their kernel release and mode-specific command line. Configure packages/services in `rootfs/config.py`, kernel overrides in `kernel/config.py`, and boot flags and dracut policy in `uki/config.py`. `live_command_line()` selects the ISO by volume label; `installed_command_line()` selects `settings.ROOT_LABEL` using GPT PARTLABEL.

Outputs:

```text
out/kernel/linux-image-*.deb
out/kernel/linux-headers-*.deb
out/rootfs-live/
out/rootfs-installed/
out/uki/BOOTX64.EFI
out/uki/uki.efi
out/iso/live/filesystem.squashfs
out/iso/payload/rootfs.squashfs
out/ubuntu_uki_iso.iso
```

The ISO contains:

```text
/boot/efi.img              FAT32 image with /EFI/BOOT/BOOTX64.EFI (live UKI)
/live/filesystem.squashfs  live system
/payload/rootfs.squashfs   installed system, including runtime modules
/payload/uki.efi           installed UKI
/payload/manifest.json     kernel release and SHA256 checksums
```

The FAT image is also appended as a GPT EFI System Partition for USB firmware. An El Torito UEFI entry supports optical/virtual CD boot. This follows [xorriso's hybrid partition options](https://www.gnu.org/software/xorriso/man_1_xorrisofs.html). Assembly checks both boot records, the ISO volume label, matching kernels/modules/command lines, embedded payload hashes and the actual EFI contents of both ESP copies before replacing the output.

`SOURCE_DATE_EPOCH` fixes SquashFS timestamps and the ISO modification timestamp. Archive package versions and other build metadata can still vary; byte-for-byte reproducibility is not guaranteed.

## Boot and install

Write `out/ubuntu_uki_iso.iso` as a raw image to the entire USB device using an image writer. Boot its UEFI entry. The live system opens a root console and mounts the live rootfs with a temporary writable overlay. The generated EFIs are unsigned; use firmware with Secure Boot disabled until you provide signed images. BIOS boot is not supported.

From the live console, inspect the disks with `lsblk`, then preview the install:

```bash
ubuntu-uki-install --root-disk /dev/nvme0n1 --raid none
# Apply after reviewing the disk and plan; asks for typed confirmation:
ubuntu-uki-install --root-disk /dev/nvme0n1 --raid none --apply
```

**Applying erases and repartitions the selected root disk.** It creates a 1 GiB FAT32 ESP and a labeled ext4 root partition, extracts the installed SquashFS, writes fstab using filesystem UUIDs, copies `uki.efi` to `/boot/efi/EFI/BOOT/BOOTX64.EFI`, and creates a firmware entry. No package installation, initramfs rebuild or compilation happens on the target. Reboot and remove the USB to boot the installed system.

The installer locates `/payload` on the mounted ISO automatically. An explicit `--payload /path/rootfs.squashfs` requires `uki.efi` and `manifest.json` beside that file. Checksums, release, modules and boot parameters are validated before partitioning. Checksums detect corruption or mismatched artifacts; they are not signatures.

Existing RAID support remains optional: `--raid reuse --data-disks "..."` checks the configured array identity before assembling it. `--raid create` requires blank members and explicit apply/confirmation. Edit array settings in `settings.py` for your hardware. With `--raid none`, no data array is managed. Installed accounts and credentials remain those of the package profile; configure them for your deployment before exposing the server.

## Validation

```bash
pytest
ruff check ubuntu_uki_iso tests
ruff format --check ubuntu_uki_iso tests
ubuntu-uki-iso test vm --test live-boot
ubuntu-uki-iso test vm --test install
```

Tests exercise real SquashFS compression/extraction and, when ISO tools are available, real xorriso assembly, optical/USB boot records, exact payload contents, module/release consistency, corruption rejection and direct EFI deployment. Disk mutations in unit tests use recording commands and temporary directories. QEMU tests use virtual disk files only.

A real kernel build and both UKI builds completed previously under `out/kernel-uki-validation` (release `7.0.14-xiahualab-iso`). The ISO assembly integration test passed with synthetic root filesystems and PE fixtures using locally extracted xorriso/mtools. A complete mmdebstrap rootfs build and boot/install/reboot VM test have not yet run in this environment; structural ISO checks do not establish boot success.
