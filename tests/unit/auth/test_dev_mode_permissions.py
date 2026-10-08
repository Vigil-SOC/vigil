"""DEV_MODE's permission dict lists what the console may show (``/api/auth/me``).

``check_permission`` grants everything in DEV_MODE, but the console reads the
dict from ``get_user_permissions``; a permission missing there hides its screen
in a dev install even though every route would allow it.
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
