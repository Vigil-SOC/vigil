"""``POST /api/findings/export`` writes one file, inside the exports directory.

The route has never returned anything but 500: it called the data service with
``format=`` where the service's parameter is ``fmt``, so every request raised
``TypeError`` before the service ran. Nothing covered the route, so nothing
said so.

The same handler builds the output filename from the query parameter without
validating it. That is less than it looks on POSIX: the caller's string is
always appended to ``findings_export_<timestamp>.``, so the first component of
any injected path is a file that does not exist, and the write fails before it
can leave the directory -- percent-encoded separators included, which the
server really does decode. It is not less than it looks on Windows, where
``\\`` splits and ``..`` cancels lexically.

What the caller does get on any platform is the extension, so the route answers
200 to ``?output_format=csv`` and hands back JSON in a file named ``.csv``.
The closed set is the fix for that, and the Windows case falls out of it.
"""

from __future__ import annotations

import inspect
import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO))

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from core.storage.database_data_service import DatabaseDataService  # noqa: E402
from services.api.errors import register_exception_handlers  # noqa: E402
from services.api.routers import findings as findings_api  # noqa: E402

pytestmark = pytest.mark.unit


class _StubDataService:
    """Carries the real signature, so a mismatched call fails here as it does live."""

    def __init__(self):
        self.calls = []

    def export_findings(self, output_path: Path, fmt: str = "json") -> bool:
        self.calls.append((output_path, fmt))
        output_path.write_text(json.dumps({"findings": []}))
        return True


@pytest.fixture
def exports_dir(tmp_path, monkeypatch):
    """`vigil_path` resolves under VIGIL_DIR, so the writes land in tmp_path."""
    monkeypatch.setenv("VIGIL_DIR", str(tmp_path))
    return tmp_path / "exports"


@pytest.fixture
def stub(monkeypatch):
    stub = _StubDataService()
    monkeypatch.setattr(findings_api, "data_service", stub)
    return stub


@pytest.fixture
def client():
    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(findings_api.router, prefix="/api/findings")
    return TestClient(app, raise_server_exceptions=False)


def test_the_stub_matches_the_service_it_stands_in_for():
    """Otherwise this file could go green while the real call still mismatches.

    Parameters only: this module's `from __future__ import annotations` makes
    its own annotations strings, and the service's are objects.
    """

    def shape(fn):
        return [
            (p.name, p.kind, p.default)
            for p in inspect.signature(fn).parameters.values()
        ]

    assert shape(_StubDataService.export_findings) == shape(
        DatabaseDataService.export_findings
    )


@pytest.mark.parametrize("fmt", ["json", "jsonl"])
def test_an_export_writes_its_file_and_answers_with_the_path(
    fmt, client, stub, exports_dir
):
    response = client.post(f"/api/findings/export?output_format={fmt}")

    assert response.status_code == 200, response.text
    written = Path(response.json()["file_path"])
    assert written.exists()
    assert written.parent == exports_dir
    assert stub.calls == [(written, fmt)]


def test_a_format_the_service_cannot_write_is_refused(client, stub, exports_dir):
    response = client.post("/api/findings/export?output_format=csv")

    assert response.status_code == 400, response.text
    assert stub.calls == []


@pytest.mark.parametrize(
    "escape",
    ["../evil", "../../evil", "json/../../evil", "/etc/evil", "\\..\\..\\evil"],
)
def test_a_format_carrying_a_path_is_refused(
    escape, client, stub, exports_dir, tmp_path
):
    response = client.post(f"/api/findings/export?output_format={escape}")

    assert response.status_code == 400, response.text
    assert stub.calls == []
    assert not (tmp_path.parent / "evil").exists()


def test_every_declared_format_is_one_the_writer_writes_differently():
    """The route's closed set and the writer's branch are one list now, but a
    format could still be added to it that the branch does not know -- which
    would answer 200 and hand back JSON in a file named after it, the exact
    thing the check exists to prevent. So write one for real in each and
    require that they differ."""
    import tempfile

    service = DatabaseDataService()
    written = {}

    with tempfile.TemporaryDirectory() as tmp:
        for fmt in DatabaseDataService.EXPORT_FORMATS:
            path = Path(tmp) / f"findings.{fmt}"
            with patch.object(
                DatabaseDataService,
                "get_findings",
                return_value=[{"finding_id": "f-1"}, {"finding_id": "f-2"}],
            ):
                assert service.export_findings(path, fmt=fmt) is True
            written[fmt] = path.read_text()

    assert len(set(written.values())) == len(written), (
        "Two declared export formats produced identical bytes, so at least one "
        f"is not a format the writer actually branches on: {sorted(written)}. "
        "Either teach export_findings to write it, or drop it from "
        "DatabaseDataService.EXPORT_FORMATS."
    )
