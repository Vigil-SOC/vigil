"""A failed save leaves every other Custom Integration listed and its secrets routed."""

import asyncio
import json
import os
from pathlib import Path

import pytest

from core.integrations import custom_integration_service as cis
from core.integrations.integration_secrets import split_secrets

FIELDS = [{"name": "api_key", "type": "password"}, {"name": "url", "type": "text"}]


@pytest.fixture
def service(tmp_path, monkeypatch):
    monkeypatch.setenv("VIGIL_DIR", str(tmp_path))
    svc = cis.CustomIntegrationService()
    for iid in ("custom-a", "custom-b"):
        assert _save(svc, iid)["success"]
    return svc


def _save(svc, iid):
    return asyncio.run(svc.save_integration(iid, {"id": iid, "fields": FIELDS}, "x = 1\n"))


def _ids(svc):
    return sorted(m["id"] for m in svc.list_custom_integrations())


def test_crash_mid_metadata_write_keeps_other_entries(service, monkeypatch):
    def torn_fsync(fd):
        os.write(fd, b"{trunc")
        raise OSError("disk full")

    with monkeypatch.context() as m:
        m.setattr(cis.os, "fsync", torn_fsync)
        assert _save(service, "custom-c") == {"success": False, "error": "disk full"}

    assert _ids(service) == ["custom-a", "custom-b"]
    assert _save(service, "custom-d")["success"]
    assert _ids(service) == ["custom-a", "custom-b", "custom-d"]
    _, plain = split_secrets("custom-a", {"api_key": "s3cret", "url": "u"})
    assert plain == {"url": "u"}


def test_failed_server_write_leaves_integration_unlisted(service, monkeypatch):
    real_open = open

    def failing_open(path, *a, **kw):
        if Path(path).name.startswith("custom-c_server"):
            raise OSError("read-only")
        return real_open(path, *a, **kw)

    monkeypatch.setattr("builtins.open", failing_open)
    assert _save(service, "custom-c")["success"] is False
    assert _ids(service) == ["custom-a", "custom-b"]


def test_corrupt_metadata_refuses_save_and_list(service):
    service.metadata_file.write_text("{not json")

    assert _save(service, "custom-c")["success"] is False
    assert service.metadata_file.read_text() == "{not json"
    assert not (service.custom_integrations_dir / "custom-c_server.py").exists()
    with pytest.raises(json.JSONDecodeError):
        service.list_custom_integrations()
