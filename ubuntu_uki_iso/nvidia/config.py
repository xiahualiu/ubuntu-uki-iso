"""
Build dependencies and runtime module selection for NVIDIA open drivers.
"""

MODULES = ("nvidia", "nvidia-modeset", "nvidia-drm", "nvidia-uvm")
TOOLS = ("make", "gcc", "g++", "ld", "dpkg-deb", "modinfo")
PACKAGES = ("build-essential", "libelf-dev", "kmod", "dpkg-dev")
