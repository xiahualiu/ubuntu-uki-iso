# Ubuntu kernel, UKI and bootstrap LiveUSB

Independent Python subpackages build Ubuntu kernel Debian packages and dracut/ukify images. The existing rootfs, ISO and server installer pipeline remains available. Python subpackages here are not Git submodules.

```text
ubuntu_uki_iso/
  kernel/     source fetching, config.py, validation, compilation, CLI
  uki/        config.py, standalone initramfs/UKI builder, live and target packaging
  build/      rootfs, squashfs, hybrid ISO orchestration
  installer/  server installation and storage checks
```

Install locally with Python 3.11 or newer:

```bash
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'
```

Configuration is defined in Python: kernel config overrides in `ubuntu_uki_iso/kernel/config.py` and validation helpers in `ubuntu_uki_iso/kernel/tests/validation.py`, UKI dracut policies and boot parameters in `ubuntu_uki_iso/uki/config.py`, and host/rootfs package profiles in `ubuntu_uki_iso/config/packages.py`. Kernel overrides use a list of config entries and a render function; UKI and package profiles use frozen dataclasses. Kernel config overrides are generated in the extracted source tree; dracut configs are written into each build's scratch space or rootfs. Edit the Python definitions rather than generated files.

## Kernel packages

```bash
ubuntu-uki-kernel --help
ubuntu-uki-kernel --temp-dir /tmp/kernel-build --jobs 32
# Equivalent without installing console scripts:
python3 -m ubuntu_uki_iso.kernel.build --temp-dir /tmp/kernel-build --jobs 32
```

Every kernel build starts by checking kernel build packages (including build-essential and development libraries) and available disk space before fetching sources. It does not install missing dependencies; errors name missing packages and an apt install command. There are no preflight-only or config-only CLI modes.

Source is downloaded with `apt-get download linux-source-7.0.0`, extracted, configured from the source tree's `x86_64_defconfig` plus project config overrides, and built with `make bindeb-pkg`. This uses the source tarball shipped in an apt **binary deb**, rather than `apt-get source`/deb-src. No separate modules package is downloaded for configuration. The source package is fixed to `linux-source-7.0.0` in settings. The base config is regenerated on every build before applying project config overrides. Use a fresh `--temp-dir` when changing source versions. The overrides include drivers detected on the current AMD host (NVMe, AHCI RAID0, Intel I226-V Ethernet, HDA audio, USB-C and Thunderbolt), plus Ubuntu core userspace and container capabilities. The profile was derived from the running `7.0.0-38-generic` config and `localmodconfig`; it is a fixed host profile, not automatic detection on each build. Nouveau is disabled; the NVIDIA driver is built separately against the custom headers. Check the resolved config when changing hardware. Kernel config validation runs separately in `ubuntu_uki_iso/kernel/tests`; builds apply the configuration and compile directly.

Outputs under `--temp-dir`: `kernel/linux-image-*.deb`, `linux-headers-*.deb`, generated configs and the build log. The actual kernel release is read from the source tree and recorded in `<temp-dir>/kernel/release`: 7.0.0 is Ubuntu's package series, not an assertion of the upstream release embedded in the tarball.

## Independent initramfs and UKI

```bash
ubuntu-uki-build --help
ubuntu-uki-build --preflight
sudo .venv/bin/ubuntu-uki-build --kernel-deb out/kernel/linux-image-RELEASE_VERSION_amd64.deb
sudo .venv/bin/ubuntu-uki-build --mode installed --kernel-deb /path/kernel.deb --output out/uki/server.efi
# Equivalent module invocation:
sudo .venv/bin/python -m ubuntu_uki_iso.uki --kernel-deb /path/kernel.deb
```

Replace the example kernel filename with your built image deb. The default input is selected from `out/kernel`. The deb is extracted into temporary build space and never installed on the host. Dracut uses the extracted kernel modules, non-host-only mode and the selected project configuration; ukify embeds the kernel, initramfs and command line. The EFI sections and embedded command line are verified before publishing the output. The initramfs is retained beside it as `*.initramfs.img` for `lsinitrd` inspection.

Default live output: `out/uki/BOOTX64.EFI` (the ISO input). Default installed output: `out/uki/installed.efi`. Use `--cmdline` to supply server boot parameters; otherwise the installed mode uses this project's fixed root PARTUUID. Both commands accept `--workspace`. Native UKI builds need Ubuntu dracut, systemd-ukify, systemd-boot-efi, kmod and zstd; dracut also needs privileges and the userland/firmware appropriate for the target. The existing build container supplies the full environment.

## Rootfs and LiveUSB ISO

Reuse the Ubuntu suite, package definitions and customization code for both rootfs images, but keep separate outputs. The live rootfs contains installer tools and live services; the server payload contains the installed system. Copying the live rootfs verbatim would also copy live-only state and configuration. A shared base with separately applied profiles is possible later; the current separate builds are easier to inspect and test.

A live UKI mounts the ISO squashfs with a writable overlay. A server UKI boots a disk root. They share a kernel but need different initramfs policy and command lines.

After building the kernel:

```bash
ubuntu-uki-iso build rootfs live
ubuntu-uki-iso build rootfs installed
# Build your live UKI independently as above, or use the rootfs-contained toolchain:
ubuntu-uki-iso build uki
# Package the installed UKI and kernel modules for the existing installer:
ubuntu-uki-iso build uki-target
ubuntu-uki-iso build squashfs
ubuntu-uki-iso build iso
```

`build uki` uses dracut/ukify through kernel-install inside the live rootfs. `build uki-target` builds the server UKI and a deployable deb; a standalone server EFI alone does not replace that installer package. ISO assembly consumes the live EFI at the default output path, both squashfs images, and the server UKI package. It produces `out/ubuntu_uki_iso.iso`, a UEFI hybrid ISO for USB media. Rootfs builds require root or a suitably privileged build container. The existing installer is hardware-specific; review settings before running it on a server.

For the complete existing pipeline, use `ubuntu-uki-iso build all`. Individual stages use `python -m ubuntu_uki_iso.kernel.build`, `python -m ubuntu_uki_iso.uki.live` and `python -m ubuntu_uki_iso.uki.target`.

## Validation

```bash
.venv/bin/python -m pytest
.venv/bin/ruff check ubuntu_uki_iso/kernel ubuntu_uki_iso/uki
```

Unit tests do not prove a kernel build or boot. Full validation requires building the artifacts and booting the hybrid image under UEFI (the existing QEMU harness is available). Previous design and verification history is preserved in [docs/original-design.md](docs/original-design.md).
