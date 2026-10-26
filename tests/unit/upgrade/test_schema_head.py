"""One Alembic head, and it fits alembic_version.version_num."""

from __future__ import annotations

import importlib.util

from core.storage.connection import schema_head
from core.upgrade.command import PACKAGE_DIR


def test_the_image_has_one_head_that_fits_the_version_column() -> None:
    head = schema_head()
    assert head == "0011_command_bar_indexes"
    assert len(head) <= 32


def test_command_bar_indexes_follow_the_autonomy_ceiling() -> None:
    path = PACKAGE_DIR / "versions" / "0011_command_bar_indexes.py"
    spec = importlib.util.spec_from_file_location("command_bar_indexes_0011", path)
    assert spec is not None and spec.loader is not None
    revision = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(revision)
    assert revision.revision == "0011_command_bar_indexes"
    assert revision.down_revision == "0010_autonomy_ceiling"
    assert isinstance(revision.down_revision, str)
