"""The destructive bulk case-clear endpoint must enforce cases.delete.

Client-side gating hides the button, but the endpoint itself must refuse a
caller without the permission — a read-only analyst must not be able to wipe
the case database by calling the API directly.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

os.environ.setdefault("DEV_MODE", "true")

REPO = Path(__file__).resolve().parent.parent.parent
for p in (str(REPO), str(REPO / "backend")):
    if p not in sys.path:
        sys.path.insert(0, p)

pytestmark = pytest.mark.unit


class _User:
    def __init__(self, user_id: str):
        self.user_id = user_id


@pytest.mark.asyncio
async def test_clear_all_cases_denied_without_permission(monkeypatch):
    from fastapi import HTTPException

    from backend.api import cases
    from backend.services.auth_service import AuthService

    monkeypatch.setattr(
        AuthService, "check_permission", staticmethod(lambda *a, **k: False)
    )

    with pytest.raises(HTTPException) as exc:
        await cases.clear_all_cases(current_user=_User("analyst-1"))

    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_clear_all_cases_checks_cases_delete(monkeypatch):
    from backend.api import cases
    from backend.services.auth_service import AuthService

    seen = {}

    def _record(user_id, permission, *a, **k):
        seen["permission"] = permission
        return False

    monkeypatch.setattr(AuthService, "check_permission", staticmethod(_record))

    with pytest.raises(Exception):
        await cases.clear_all_cases(current_user=_User("analyst-1"))

    assert seen["permission"] == "cases.delete"
