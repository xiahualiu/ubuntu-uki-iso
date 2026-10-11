"""Dependencies and sizing policy for hybrid ISO assembly."""


def required_packages() -> tuple[str, ...]:
    """Return the packages required to assemble and inspect the ISO."""
    return ("apt", "xorriso", "dosfstools", "mtools", "squashfs-tools")


def efi_image_size(size: int) -> int:
    """Return FAT32 image size in bytes, allowing space for metadata and the UKI."""
    return max(40, (size + 1024**2 - 1) // 1024**2 + 16) * 1024**2
