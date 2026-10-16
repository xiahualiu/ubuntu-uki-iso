"""
Check the installer validates the GPT label used by the installed UKI.
"""

from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock

import pytest

from ubuntu_uki_iso import settings
from ubuntu_uki_iso.errors import BuildError
from ubuntu_uki_iso.installer import partition


@pytest.mark.parametrize("label", [settings.ROOT_LABEL, "wrong-root"])
def test_root_partition_label_is_verified(monkeypatch: pytest.MonkeyPatch, label: str) -> None:
    root = SimpleNamespace(name=label)
    table = Mock()
    table.by_number.return_value = root
    monkeypatch.setattr(partition.gpt, "parse", Mock(return_value=table))
    ctx = Mock(root_disk="/dev/test")
    if label == settings.ROOT_LABEL:
        partition._verify_root_partition(ctx)
    else:
        with pytest.raises(BuildError, match="PARTLABEL"):
            partition._verify_root_partition(ctx)


def test_real_sgdisk_writes_the_root_label_to_a_disk_image(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import shutil
    import subprocess

    from ubuntu_uki_iso.installer.context import Context
    from ubuntu_uki_iso.installer.preflight import RaidMode

    if not shutil.which("sgdisk"):
        pytest.skip("sgdisk unavailable")
    image = tmp_path / "root.img"
    with image.open("wb") as stream:
        stream.truncate(128 * 1024**2)
    execute = subprocess.run

    def command(argv: list[Any], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        if argv[0] == "sgdisk":
            return execute(argv, **kwargs)
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(partition.subprocess, "run", command)
    monkeypatch.setattr(partition.device, "settle_udev", lambda: None)
    monkeypatch.setattr(partition.device, "exists", lambda path: True)
    ctx = Context(str(image), [], RaidMode.NONE, dry_run=False, confirmed=True, esp_size="16M")
    partition.run(ctx)
    table = partition.gpt.parse(image)
    esp, root = table.by_number(1), table.by_number(2)
    assert esp is not None and esp.name == "ESP"
    assert root is not None and root.name == settings.ROOT_LABEL
