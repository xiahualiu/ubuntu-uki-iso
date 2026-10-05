# Kernel profile for the Ryzen workstation

The profile targets the detected Ryzen 9 5900XT, NVMe root disk, AHCI RAID0,
Intel I226-V Ethernet, HDA audio, Thunderbolt and USB-C hardware. Hardware and
application requirements are validated separately in kernel/tests/validation.py.

There is no universal minimum number of drivers. A fixed PCI inventory does not
cover applications or occasionally connected USB devices. Linux's
[trimmed-kernel guide](https://cdn.kernel.org/doc/html/latest/admin-guide/quickly-build-trimmed-linux.html)
warns that localmodconfig can discard disconnected peripherals and unused
virtualization features. It is used as an inventory aid, not the complete
workstation specification.

## Workload requirements

- Rust userspace development retains executable loading, networking, futexes,
  event polling, filesystem notifications, async I/O, pseudoterminals, profiling
  and FUSE. Rust's
  [Linux platform requirements](https://doc.rust-lang.org/rustc/platform-support.html)
  do not require CONFIG_RUST; that option enables Rust code inside the kernel.
- Wine retains IA-32 compatibility for older Wine installations, audio and input,
  and the [NTSYNC driver](https://docs.kernel.org/userspace-api/ntsync.html).
  Controller support includes evdev, joydev, uinput, UHID, Xbox, Sony, Nintendo
  and Steam HID support. NTSYNC availability is a kernel prerequisite; the Wine
  build and device permissions also determine whether it is used.
- Docker retains namespace isolation, cgroups and resource controllers, seccomp,
  OverlayFS, ext4 ACLs, and IPv4/IPv6 firewall/NAT support. Networking includes
  bridges, VLANs, veth, VXLAN, macvlan, ipvlan and IPVS. The requirements are
  based on [Moby's config checker](https://github.com/moby/moby/blob/master/contrib/check-config.sh)
  and [Docker's firewall documentation](https://docs.docker.com/engine/network/packet-filtering-firewalls/).
- External NVIDIA modules retain module loading, matching headers, DRM and
  DRM_KMS_HELPER, as required by
  [NVIDIA's DRM KMS documentation](https://download.nvidia.com/XFree86/Linux-x86_64/595.99.02/README/kms.html).
  Nouveau stays disabled. The separately built NVIDIA driver and userspace
  graphics libraries still need testing against this kernel.

Common USB serial adapters, CDC ACM devices, USB audio, UVC webcams, USB mass
storage, exFAT and NTFS3 are enabled even when absent from the boot-time inventory.
AMD KVM and vhost networking are retained for optional development VMs; Wine and
native Docker do not require KVM.

## Steam and Proton

Retain IA-32 compatibility, futex/eventfd synchronization and NTSYNC, DRM/KMS,
shared-buffer synchronization, ALSA audio and Steam Input interfaces. Controller
support includes PlayStation, Nintendo, Xbox rumble/LEDs, Steam controllers,
Logitech receivers and USB force feedback. The multicolor LED dependency must be
enabled for modern PlayStation and Logitech drivers to survive Kconfig resolution.

[Valve's Proton requirements](https://github.com/ValveSoftware/Proton/wiki/Requirements)
also require suitable userspace graphics drivers.
[Steam's Linux client](https://github.com/ValveSoftware/steam-for-linux/blob/master/README.md)
requires both 32-bit and 64-bit graphics libraries.
[Valve's Steam Input rules](https://github.com/ValveSoftware/steam-devices/blob/master/60-steam-input.rules)
provide uinput/hidraw device access. These rules and the NVIDIA Vulkan/OpenGL
libraries are userspace dependencies, not additional kernel modules. Kernel
checks alone cannot guarantee compatibility of every game or its anti-cheat.

## Building this kernel on the workstation

The kernel retains the executable, process, terminal and filesystem interfaces
needed to run GCC, make and Debian packaging tools. Module loading and symbol
versioning are also enabled for separately built drivers. These are checked as
an explicit native-build requirement group; no additional hardware drivers are
needed solely to compile a kernel.

Install the userspace dependencies returned by kernel/build.py:required_packages,
including build-essential, bison, flex, bc, libssl-dev, libelf-dev, libdw-dev,
libncurses-dev, dwarves and Debian packaging tools. The current machine passes
that preflight check. A native build under the newly built kernel still needs
verification after booting it; builds performed so far ran under Ubuntu's stock
kernel.

## Preserved exclusions and validation limits

Intel CPU-only support is disabled, but Intel-named peripheral and cross-vendor
crypto/power drivers used on this AMD machine remain. AMD microcode is enabled.
AppArmor and SELinux remain disabled as explicitly requested. Wi-Fi and Bluetooth
remain outside this fixed host profile.

Validation checks the resolved .config after olddefconfig, not just the requested
overrides. The audit is saved under out/kernel-test-32/host-detection/.
A successful compilation/config audit does not substitute for booting this kernel
and testing real Cargo workloads, Wine applications, Docker networking and the
external NVIDIA modules. Existing host application tests would exercise the stock
running kernel instead.
