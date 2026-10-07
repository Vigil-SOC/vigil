"""S3 listing failures must raise, not look like an empty bucket (#1577)."""

from __future__ import annotations

from datetime import datetime, timezone

import boto3
import pytest
from botocore.stub import Stubber
from fastapi import FastAPI
from fastapi.testclient import TestClient

from core.storage.s3_service import S3Service
from services.api.routers import config as config_api
from services.api.routers import ingestion as ingestion_api

pytestmark = pytest.mark.unit

BUCKET = "vigil-bucket"


def _service(stubber_setup) -> S3Service:
    """S3Service whose client is a botocore Stubber primed by ``stubber_setup``."""
    svc = S3Service(BUCKET, aws_access_key_id="x", aws_secret_access_key="y")
    svc.s3_client = boto3.client(
        "s3", region_name="us-east-1", aws_access_key_id="x", aws_secret_access_key="y"
    )
    stubber = Stubber(svc.s3_client)
    stubber_setup(stubber)
    stubber.activate()
    return svc


def _denied(stubber: Stubber, n: int = 1) -> None:
    for _ in range(n):
        stubber.add_client_error(
            "list_objects_v2", service_error_code="AccessDenied", http_status_code=403
        )


def _empty(stubber: Stubber) -> None:
    stubber.add_response("list_objects_v2", {"IsTruncated": False})


@pytest.mark.parametrize("method", ["list_files", "list_files_detailed"])
def test_empty_listing_returns_empty_list(method):
    svc = _service(_empty)
    assert getattr(svc, method)("p/") == []


def test_listing_returns_keys():
    def setup(stubber):
        stubber.add_response(
            "list_objects_v2",
            {
                "IsTruncated": False,
                "Contents": [
                    {
                        "Key": "p/a.csv",
                        "Size": 3,
                        "LastModified": datetime(2026, 1, 1, tzinfo=timezone.utc),
                    }
                ],
            },
        )

    assert _service(setup).list_files("p/") == ["p/a.csv"]


@pytest.mark.parametrize("method", ["list_files", "list_files_detailed"])
@pytest.mark.parametrize("code", ["AccessDenied", "NoSuchBucket"])
def test_list_errors_raise_with_code(method, code):
    def setup(stubber):
        stubber.add_client_error(
            "list_objects_v2", service_error_code=code, http_status_code=403
        )

    svc = _service(setup)
    with pytest.raises(Exception) as exc:
        getattr(svc, method)()
    assert code in str(exc.value)


def test_uninitialized_client_raises_with_cause():
    svc = S3Service(BUCKET)
    svc.s3_client = None
    svc._init_error = "boom"
    with pytest.raises(RuntimeError, match="boom"):
        svc.list_files()


def test_connection_test_fails_on_denied_listing(monkeypatch):
    svc = _service(lambda s: (s.add_response("head_bucket", {}), _denied(s)))
    cfg = {"bucket_name": BUCKET, "parquet_prefix": "findings/"}

    class _Cfg:
        def get_integration_config(self, _name):
            return {"config": cfg}

    monkeypatch.setattr(config_api, "get_config_service", lambda: _Cfg())
    monkeypatch.setattr(config_api, "get_secret", lambda _k: "secret")
    monkeypatch.setattr(config_api, "S3Service", lambda **_kw: svc)

    result = config_api.test_s3_connection()

    assert result["success"] is False
    assert "AccessDenied" in result["message"]
    assert "files_found" not in result


@pytest.fixture
def ingest_client(monkeypatch):
    svc = _service(lambda s: _denied(s, 2))
    monkeypatch.setattr(ingestion_api, "_get_s3_service", lambda: svc)
    app = FastAPI()
    app.include_router(ingestion_api.router, prefix="/api/ingest")
    with TestClient(app) as client:
        yield client


def test_s3_files_endpoint_errors_on_denied_listing(ingest_client):
    resp = ingest_client.get("/api/ingest/s3-files")
    assert resp.status_code == 403
    assert "AccessDenied" in resp.json()["detail"]


def test_sync_s3_folder_errors_on_denied_listing(ingest_client):
    resp = ingest_client.post("/api/ingest/sync-s3-folder", params={"prefix": "p/"})
    assert resp.status_code == 403
    assert "AccessDenied" in resp.json()["detail"]
