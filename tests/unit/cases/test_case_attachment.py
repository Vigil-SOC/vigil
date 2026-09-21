"""A Human Ask can carry an already-stored document onto the Case (#1010)."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from core.cases.case_records_service import add_attachment
from core.storage.models import CaseAttachment

pytestmark = pytest.mark.unit


def test_add_attachment_points_the_case_at_the_stored_object():
    session = MagicMock()

    attachment = add_attachment(session, "case-1", document_id="doc-9")

    session.add.assert_called_once_with(attachment)
    session.flush.assert_called_once()
    assert isinstance(attachment, CaseAttachment)
    assert attachment.case_id == "case-1"
    assert attachment.file_path == "doc-9"
    assert attachment.filename == "doc-9"
    assert attachment.uploaded_by == "human_ask"
    assert attachment.file_size == 0
