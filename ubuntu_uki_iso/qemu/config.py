"""
Simulated workstation resources and VM timeouts.
"""

VM_MEMORY_MB = 4096
VM_CPUS = 4
VM_BOOT_TIMEOUT = 900
VM_COMMAND_TIMEOUT = 1800
VM_ROOT_DISK_SIZE = 1_000_000_000_000
VM_DATA_DISK_SIZE = 8_000_000_000_000
VM_DATA_DISKS = 8
AHCI_PORTS = 6


def required_packages() -> tuple[str, ...]:
    """
    Return host packages needed for LiveUSB VM checks.
    """
    return ("python3", "qemu-system-x86", "qemu-utils", "ovmf", "expect")
