"""
Dependencies and sizing policy for hybrid ISO assembly.
"""


def required_packages() -> tuple[str, ...]:
    """
    Return the packages required to assemble and inspect the ISO.
    """
    return ("apt", "dpkg", "xorriso", "dosfstools", "mtools", "squashfs-tools")


def efi_image_size(size: int) -> int:
    """
    Return FAT32 image size in bytes, allowing space for metadata and the UKI.

    FAT32's standard minimum is 65,525 data clusters: about 32 MiB of data
    with 512-byte clusters, plus filesystem metadata. Although mkfs.fat -F 32
    can force a smaller filesystem, it warns when the cluster count is below
    that minimum. The 40 MiB floor is a conservative project policy rather
    than an exact FAT32 size requirement. Larger UKIs get their size rounded
    up to MiB plus 16 MiB of headroom.

    Reference: https://github.com/dosfstools/dosfstools/blob/master/src/mkfs.fat.c
    """
    return max(40, (size + 1024**2 - 1) // 1024**2 + 16) * 1024**2
