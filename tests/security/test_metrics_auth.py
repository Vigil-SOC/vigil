"""GET /metrics on the API port takes a static bearer, not a user session.

An unset token refuses every caller, including DEV_MODE. Compose shares the
token with Prometheus through a file; that wiring is checked here too, without
starting the stack.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest
import yaml
from prometheus_client import CONTENT_TYPE_LATEST

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

os.environ.setdefault("JWT_SECRET_KEY", "test-only-secret-not-for-prod")

from fastapi.testclient import TestClient  # noqa: E402

pytestmark = pytest.mark.unit

COMPOSE = REPO / "infra" / "docker" / "docker-compose.yml"
PROMETHEUS = REPO / "infra" / "docker" / "prometheus.yml"
TOKEN = "metrics-scrape-token"
FILE_TOKEN = "metrics-file-token"


@pytest.fixture(scope="module")
def client():
    from services.api.main import app

    with TestClient(app) as c:
        yield c


def test_no_credential_is_401(client, monkeypatch):
    monkeypatch.delenv("METRICS_TOKEN", raising=False)
    monkeypatch.delenv("METRICS_TOKEN_FILE", raising=False)
    response = client.get("/metrics")
    assert response.status_code == 401
    assert "process_" not in response.text


def test_wrong_credential_is_401(client, monkeypatch):
    monkeypatch.setenv("METRICS_TOKEN", TOKEN)
    response = client.get("/metrics", headers={"Authorization": "Bearer nope"})
    assert response.status_code == 401
    assert "process_" not in response.text


def test_configured_credential_is_prometheus_text(client, monkeypatch):
    monkeypatch.setenv("METRICS_TOKEN", TOKEN)
    response = client.get("/metrics", headers={"Authorization": f"Bearer {TOKEN}"})
    assert response.status_code == 200
    assert response.headers["content-type"] == CONTENT_TYPE_LATEST
    assert "process_" in response.text


def test_unset_token_refuses_dev_mode(client, monkeypatch):
    from core.auth import current_user as current_user_module

    monkeypatch.delenv("METRICS_TOKEN", raising=False)
    monkeypatch.delenv("METRICS_TOKEN_FILE", raising=False)
    monkeypatch.setattr(current_user_module, "DEV_MODE", True)
    response = client.get("/metrics")
    assert response.status_code == 401


def test_env_wins_over_file(client, monkeypatch, tmp_path):
    path = tmp_path / "token"
    path.write_text(FILE_TOKEN + "\n")
    monkeypatch.setenv("METRICS_TOKEN", TOKEN)
    monkeypatch.setenv("METRICS_TOKEN_FILE", str(path))
    file_auth = {"Authorization": f"Bearer {FILE_TOKEN}"}
    env_auth = {"Authorization": f"Bearer {TOKEN}"}
    assert client.get("/metrics", headers=file_auth).status_code == 401
    assert client.get("/metrics", headers=env_auth).status_code == 200


def test_file_token_is_accepted_when_env_is_unset(client, monkeypatch, tmp_path):
    path = tmp_path / "token"
    path.write_text(FILE_TOKEN + "\n")
    monkeypatch.delenv("METRICS_TOKEN", raising=False)
    monkeypatch.setenv("METRICS_TOKEN_FILE", str(path))
    response = client.get("/metrics", headers={"Authorization": f"Bearer {FILE_TOKEN}"})
    assert response.status_code == 200
    assert response.headers["content-type"] == CONTENT_TYPE_LATEST


def test_compose_shares_the_bearer_with_prometheus():
    services = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))["services"]
    backend = services["backend"]
    assert backend["environment"]["METRICS_TOKEN_FILE"] == "/run/vigil/metrics/token"
    assert "metrics_token:/run/vigil/metrics:ro" in backend["volumes"]
    assert "metrics-token" in backend["depends_on"]

    writer = services["metrics-token"]
    script = writer["entrypoint"][-1]
    assert "0444" in script
    assert "ports" not in writer
    assert writer["network_mode"] == "none"
    assert "metrics_token:/run/vigil/metrics" in writer["volumes"]

    prometheus = services["prometheus"]
    assert "metrics_token:/run/vigil/metrics:ro" in prometheus["volumes"]
    assert "metrics-token" in prometheus["depends_on"]
    # The daemon's listener stays a separate scrape, with no new published port.
    assert prometheus["ports"] == ["9095:9090"]

    jobs = {
        job["job_name"]: job
        for job in yaml.safe_load(PROMETHEUS.read_text(encoding="utf-8"))[
            "scrape_configs"
        ]
    }
    assert (
        jobs["backend"]["authorization"]["credentials_file"]
        == "/run/vigil/metrics/token"
    )
    assert "authorization" not in jobs["soc-daemon"]
