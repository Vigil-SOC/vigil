"""A caller that is not Vigil, with a credential Vigil issued, reaching a tool.

The gate tests stop at the gate: they patch ``authenticate`` and assert on the
answer it produces. This one mints a real credential, presents it over HTTP from
a host that is not localhost, and drives the MCP handshake through to
``tools/list`` -- the path an external client actually walks.

Storage is SQLite: what is under test is the surface, not the dialect. It is
built thread-safe on purpose, because the test client serves the app on another
thread and ``authenticate`` answers None for every failure, including one that
is really a connection used from the wrong place.
"""

from __future__ import annotations

from contextlib import contextmanager
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from core.auth import mcp_credential_service as credentials
from core.storage.models import McpCredential, Role, User
from core.storage.models.base import Base

pytestmark = pytest.mark.unit


# `users` and `roles` carry JSONB columns this has no interest in, and SQLite
# cannot render the type at all. Registered for the SQLite dialect only.
@compiles(JSONB, "sqlite")
def _jsonb_is_json_on_sqlite(type_, compiler, **kw):
    return "JSON"


@pytest.fixture
def issued_credential():
    """A credential the service really minted, and the store it lives in."""
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(
        engine, tables=[Role.__table__, User.__table__, McpCredential.__table__]
    )
    session = sessionmaker(bind=engine)()
    session.add(
        Role(role_id="r-analyst", name="analyst", description="", permissions={})
    )
    session.add(
        User(
            user_id="u-1",
            username="nestor",
            email="nestor@example.com",
            password_hash="x",
            full_name="Nestor",
            role_id="r-analyst",
            is_active=True,
        )
    )
    session.commit()

    minted = credentials.mint("u-1", "the platform", session=session)

    @contextmanager
    def _this_store(_=None):
        yield session

    with patch("core.auth.mcp_credential_service.unit_of_work", _this_store):
        yield minted.token

    session.close()


@pytest.fixture
def client_on_a_domain():
    from fastapi.testclient import TestClient

    from services.api.main import app

    with TestClient(app, base_url="http://vigil.example.com") as c:
        yield c


def test_a_credential_vigil_issued_reaches_the_tools(
    client_on_a_domain, issued_credential
):
    headers = {
        "Authorization": f"Bearer {issued_credential}",
        "Accept": "application/json, text/event-stream",
    }

    with patch("services.api.mcp_surface.is_enabled", return_value=True):
        opened = client_on_a_domain.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "not-vigil", "version": "1"},
                },
            },
            headers=headers,
        )
        assert opened.status_code == 200, opened.text
        session_id = opened.headers["mcp-session-id"]

        client_on_a_domain.post(
            "/mcp",
            json={"jsonrpc": "2.0", "method": "notifications/initialized"},
            headers={**headers, "mcp-session-id": session_id},
        )
        listed = client_on_a_domain.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            headers={**headers, "mcp-session-id": session_id},
        )

    assert listed.status_code == 200, listed.text
    assert "list_findings" in listed.text, (
        "The surface answered, but not with Vigil's tools. The handshake "
        "completed and tools/list returned nothing recognisable."
    )
