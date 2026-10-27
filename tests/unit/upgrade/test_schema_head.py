"""One Alembic head, and it fits alembic_version.version_num."""

from __future__ import annotations

import importlib.util

from core.storage.connection import schema_head
from core.upgrade.command import PACKAGE_DIR


def test_the_image_has_one_head_that_fits_the_version_column() -> None:
    head = schema_head()
    assert head == "0017_log_events"
    assert len(head) <= 32


def test_the_merge_joins_both_0010_revisions() -> None:
    path = PACKAGE_DIR / "versions" / "0011_merge_0010_heads.py"
    spec = importlib.util.spec_from_file_location("merge_0010_heads", path)
    assert spec is not None and spec.loader is not None
    revision = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(revision)
    assert revision.down_revision == (
        "0010_autonomy_ceiling",
        "0010_command_bar_search_indexes",
    )
    indexes = PACKAGE_DIR / "versions" / "0010_command_bar_search_indexes.py"
    spec = importlib.util.spec_from_file_location("command_bar_search_indexes", indexes)
    assert spec is not None and spec.loader is not None
    indexes_rev = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(indexes_rev)
    assert indexes_rev.revision == "0010_command_bar_search_indexes"
    assert len(indexes_rev.revision) <= 32


def test_the_merge_joins_both_0012_revisions() -> None:
    path = PACKAGE_DIR / "versions" / "0013_merge_0012_heads.py"
    spec = importlib.util.spec_from_file_location("merge_0012_heads", path)
    assert spec is not None and spec.loader is not None
    revision = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(revision)
    assert revision.down_revision == (
        "0012_audit_feed_pause",
        "0012_closure_channel",
    )
    assert len(revision.revision) <= 32
