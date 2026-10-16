"""
Check optional data-array mount policy in the installed system's fstab.
"""

from pathlib import Path

import pytest

from ubuntu_uki_iso.installer import context, target
from ubuntu_uki_iso.installer.context import Context
from ubuntu_uki_iso.installer.preflight import RaidMode


@pytest.mark.parametrize("data_uuid", ["", "data-array-uuid"])
def test_data_array_uses_existing_options_and_is_optional_at_boot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, data_uuid: str
) -> None:
    monkeypatch.setattr(context, "TARGET_MOUNT", tmp_path)
    (tmp_path / "etc").mkdir()
    ctx = Context(
        "/dev/test",
        [],
        RaidMode.NONE,
        root_uuid="root-uuid",
        esp_uuid="esp-uuid",
        data_uuid=data_uuid,
    )
    target._write_fstab(ctx)
    entries = [
        line.split()
        for line in (tmp_path / "etc/fstab").read_text().splitlines()
        if line and not line.startswith("#")
    ]
    data_entries = [entry for entry in entries if entry[1] == "/mnt/raid0"]
    assert len(data_entries) == bool(data_uuid)
    if data_uuid:
        assert data_entries[0] == [
            f"UUID={data_uuid}",
            "/mnt/raid0",
            "ext4",
            "defaults,noatime,nodiratime,data=writeback,barrier=0,commit=600,nofail,x-systemd.device-timeout=10s",
            "0",
            "2",
        ]
    assert next(entry for entry in entries if entry[1] == "/")[3] == "defaults"
