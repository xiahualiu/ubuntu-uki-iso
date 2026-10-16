"""
Validate ISO-specific boot metadata.
"""


def parse_el_torito(text: str) -> bool:
    """
    Whether xorriso's El Torito report contains a bootable UEFI entry.

    Takes the text of ``xorriso -report_el_torito plain``. The line looks like::

        El Torito boot img :   1  UEFI  y   none  0x0000  0x00      0    35

    so this looks for the platform and the bootable flag, in that order and on
    the same line, rather than for the substring "efi" — which also matches the
    image path ``/boot/efi.img`` and would pass on an image with no UEFI entry
    at all.
    """
    for line in text.splitlines():
        if "UEFI" not in line:
            continue
        tail = line.split("UEFI", 1)[1].split()
        if tail and tail[0].lower() == "y":
            return True
    return False
