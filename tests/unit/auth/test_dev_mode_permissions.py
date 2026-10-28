"""DEV_MODE's permission dict lists what the console may show (``/api/auth/me``).

Both the dict and ``check_permission`` are built-in Admin from the catalog. A
name that role does not list is denied. The console reads the dict, so a
permission missing there hides its screen.
"""

import pytest

from core.auth.auth_service import AuthService
from core.auth.permissions import MEDIC_ADMIN_PERMISSION, MEDIC_READ_PERMISSION

pytestmark = pytest.mark.unit


def test_dev_mode_lists_the_medic_permissions(monkeypatch):
    monkeypatch.setattr("core.auth.auth_service._is_dev_mode", lambda: True)

    permissions = AuthService.get_user_permissions("anyone")

    assert permissions[MEDIC_READ_PERMISSION] is True
    assert permissions[MEDIC_ADMIN_PERMISSION] is True
    assert AuthService.check_permission("anyone", MEDIC_READ_PERMISSION) is True
    assert AuthService.check_permission("anyone", "not.a.permission") is False
