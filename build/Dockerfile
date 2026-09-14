# syntax=docker/dockerfile:1
#
# Build environment for ubuntu-uki-iso.
#
# Why this exists at all: the build host has no passwordless sudo. Every step
# that needs root — mmdebstrap, chroot, kernel-install, mount — runs in here
# instead. That is a hard constraint of this project, not a preference.
#
# The `build` stage is the default and is what every `make` target uses. The
# `test` stage adds QEMU and OVMF and is only used by `make test-vm`.

FROM ubuntu:26.04 AS build

ENV DEBIAN_FRONTEND=noninteractive
ENV LC_ALL=C.UTF-8

# --- Kernel build -----------------------------------------------------------
# flex/bison/bc/libssl/libelf/libncurses are the standard kernel dep set.
# dwarves provides pahole, which is only needed because CONFIG_DEBUG_INFO_BTF
# stays on (see ubuntu_uki_iso/data/kernel/config-trim.fragment on why it leaves it
# alone). debhelper/fakeroot/dpkg-dev/build-essential are what `make
# bindeb-pkg` shells out to via scripts/package/mkdebian.
#
# --- Image build ------------------------------------------------------------
# mmdebstrap bootstraps the rootfs; xorriso makes the hybrid ISO; mtools and
# dosfstools build the FAT EFI image *without* needing a loop device or a
# mount, which is what lets the whole thing run unprivileged-ish in here.
#
# --- Live boot --------------------------------------------------------------
# python3 is spelled out rather than left to arrive as a dependency of
# mmdebstrap: every build step in this project *is* a Python module, so the
# interpreter is the runtime, not a transitive detail. It is also the one
# package whose absence would be reported as "no module named ubuntu-uki-iso" from
# inside a container, which is a confusing way to learn that.
#
# dracut is both the build-time initramfs generator and the live-boot
# implementation. systemd-ukify assembles the UKI; systemd-boot-efi provides
# /usr/lib/systemd/boot/efi/linuxx64.efi.stub, which is the PE stub the UKI is
# built around. That package is NOT installed on the host — it is the one
# missing piece the design calls out. It ships the stub without installing a
# bootloader, which is exactly what a no-bootloader design wants.

RUN apt-get update && apt-get install -y --no-install-recommends \
      build-essential \
      bc \
      bison \
      flex \
      libssl-dev \
      libelf-dev \
      libncurses-dev \
      dwarves \
      zstd \
      xz-utils \
      bzip2 \
      cpio \
      kmod \
      rsync \
      debhelper \
      fakeroot \
      dpkg-dev \
      devscripts \
      equivs \
      git \
      wget \
      curl \
      ca-certificates \
      gnupg \
      ubuntu-keyring \
      python3 \
      mmdebstrap \
      xorriso \
      squashfs-tools \
      mtools \
      dosfstools \
      sbsigntool \
      dracut \
      systemd-ukify \
      systemd-boot-efi \
      shellcheck \
      file \
      less \
      vim-tiny \
    && rm -rf /var/lib/apt/lists/*

# Fail loudly if the UKI stub is ever missing from an image — every argument
# about this project's boot chain assumes it is present.
RUN test -f /usr/lib/systemd/boot/efi/linuxx64.efi.stub \
    || { echo "FATAL: linuxx64.efi.stub missing; systemd-boot-efi did not provide it" >&2; exit 1; }

WORKDIR /work

FROM build AS test

# QEMU is the only way to prove the installer is non-destructive without
# pointing it at 58 TB of real data. ovmf gives it UEFI firmware, so the VM
# boots the ISO the same way the machine does.
RUN apt-get update && apt-get install -y --no-install-recommends \
      qemu-system-x86 \
      ovmf \
      expect \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /work
