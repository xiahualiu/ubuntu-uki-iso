"""Configuration: what the image is made of.

This subpackage reads the packaged data files and answers questions about
them. It holds the validation logic too — the checks that a trim fragment
names real kernel symbols and that nothing on the never-disable list is
disabled — because those checks are the difference between a kernel that runs
containers and one that merely builds.
"""

from __future__ import annotations

from .boot import (
    cmdline_installed,
    cmdline_live,
    dracut_conf,
)
from .kernel import (
    GuardEntry,
    check_guard,
    fragment_symbols,
    install_conf,
    never_disable,
    nodebug_fragment,
    parse_config_values,
    parse_guard,
    trim_fragment,
    unknown_fragment_symbols,
)
from .packages import PackageList, host_packages, package_list

__all__ = [
    "GuardEntry",
    "PackageList",
    "check_guard",
    "cmdline_installed",
    "cmdline_live",
    "dracut_conf",
    "fragment_symbols",
    "host_packages",
    "install_conf",
    "never_disable",
    "nodebug_fragment",
    "package_list",
    "parse_config_values",
    "parse_guard",
    "trim_fragment",
    "unknown_fragment_symbols",
]
