"""Check the installer validates the GPT label used by the installed UKI."""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from ubuntu_uki_iso import settings
from ubuntu_uki_iso.errors import BuildError
from ubuntu_uki_iso.installer import partition


@pytest.mark.parametrize("label", [settings.ROOT_LABEL, "wrong-root"])
def test_root_partition_label_is_verified(monkeypatch, label):
    root = SimpleNamespace(name=label)
    table = Mock()
    table.by_number.return_value = root
    monkeypatch.setattr(partition.gpt, "parse", Mock(return_value=table))
    ctx = Mock(root_disk="/dev/test", console=Mock())
    if label == settings.ROOT_LABEL:
        partition._verify_root_partition(ctx)
        ctx.console.ok.assert_called_once()
    else:
        with pytest.raises(BuildError, match="PARTLABEL"):
            partition._verify_root_partition(ctx)
