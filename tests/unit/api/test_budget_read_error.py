from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from services.api.routers import budgets

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    "path", ["/api/analytics/budget", "/api/analytics/budget/quota"]
)
def test_budget_reads_answer_5xx_when_settings_read_fails(path):
    app = FastAPI()
    app.include_router(budgets.router, prefix="/api")
    client = TestClient(app, raise_server_exceptions=False)
    with patch(
        "core.llm.cost.budget.get_session", side_effect=RuntimeError("DB exploded")
    ):
        r = client.get(path)
    assert r.status_code == 500, f"answered {r.status_code} {r.text}"
