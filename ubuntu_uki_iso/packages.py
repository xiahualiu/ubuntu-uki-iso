"""
Aggregate host requirements declared by the individual submodules.
"""

from .errors import ConfigError


def host_packages(*groups: str) -> tuple[str, ...]:
    """
    Combine requested package groups, preserving order and removing duplicates.

    Build requirements come from each stage; QEMU owns the test requirements.
    """
    from .iso.config import required_packages as iso_packages
    from .kernel.config import required_packages as kernel_packages
    from .qemu.config import required_packages as qemu_packages
    from .rootfs.config import required_packages as rootfs_packages
    from .uki.config import required_packages as uki_packages

    profiles = {
        "build": (
            "python3",
            *kernel_packages(),
            *uki_packages(),
            *rootfs_packages(),
            *iso_packages(),
        ),
        "test": qemu_packages(),
    }
    wanted = groups or ("build",)
    unknown = [group for group in wanted if group not in profiles]
    if unknown:
        raise ConfigError(f"unknown host package group(s): {', '.join(unknown)}")
    return tuple(dict.fromkeys(package for group in wanted for package in profiles[group]))
