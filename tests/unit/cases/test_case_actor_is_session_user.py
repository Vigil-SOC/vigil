"""Case comments, evidence, custody and links name the signed-in user.

Those routes used to store the actor the request body carried, so any
``cases.write`` holder could write as someone else, and the console stored
every row as ``SOC Analyst``. These run against the mounted app so the
router's auth dependency is the real one; the services are stubs.
"""

from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from core.cases import case_records_service
from core.cases.case_collaboration_service import CaseCollaborationService
from core.cases.case_evidence_service import CaseEvidenceService
from core.routing import request_unit_of_work
from core.storage.models import CaseComment, CaseEvidence, CaseRelationship, User
from services.api import main as backend_main
from services.api.middleware import auth as auth_module

pytestmark = pytest.mark.unit

WHEN = datetime(2026, 10, 21)


@pytest.fixture()
def alice(monkeypatch):
    """Signed in as alice, holding cases.write. Records what each service stored."""
    user = User(
        user_id="alice",
        username="alice",
        email="alice@test.local",
        password_hash="",
        role_id="role-analyst",
        is_active=True,
        mfa_enabled=False,
    )
    monkeypatch.setattr(
        "core.auth.auth_service.AuthService.check_permission",
        lambda *args, **kwargs: True,
    )
    recorded: dict = {}

    def add_comment(
        self, case_id, author, content, parent_comment_id=None, session=None
    ):
        recorded["comment"] = author
        return CaseComment(
            comment_id=1,
            case_id=case_id,
            author=author,
            content=content,
            parent_comment_id=parent_comment_id,
            mentions=[],
            attachment_ids=[],
            is_edited=False,
            is_deleted=False,
            created_at=WHEN,
            updated_at=WHEN,
        )

    def add_evidence(self, **kwargs):
        recorded["evidence"] = kwargs["collected_by"]
        return CaseEvidence(
            evidence_id=7,
            case_id=kwargs["case_id"],
            evidence_type=kwargs["evidence_type"],
            name=kwargs["name"],
            description=kwargs.get("description"),
            collected_by=kwargs["collected_by"],
            collected_at=WHEN,
            chain_of_custody=[],
            tags=kwargs.get("tags") or [],
            created_at=WHEN,
            updated_at=WHEN,
        )

    def add_custody(self, evidence_id, action, user, notes=None, session=None):
        recorded["custody"] = user
        return True

    def add_relationship(
        session,
        case_id,
        *,
        related_case_id,
        relationship_type,
        created_by,
        notes=None,
    ):
        recorded["relationship"] = created_by
        return CaseRelationship(
            relationship_id=3,
            case_id=case_id,
            related_case_id=related_case_id,
            relationship_type=relationship_type,
            created_by=created_by,
            notes=notes,
            created_at=WHEN,
        )

    monkeypatch.setattr(CaseCollaborationService, "add_comment", add_comment)
    monkeypatch.setattr(CaseEvidenceService, "add_evidence", add_evidence)
    monkeypatch.setattr(CaseEvidenceService, "add_chain_of_custody_entry", add_custody)
    monkeypatch.setattr(case_records_service, "add_relationship", add_relationship)

    app = backend_main.app
    app.dependency_overrides[auth_module.get_current_user] = lambda: user
    app.dependency_overrides[auth_module.get_current_active_user] = lambda: user
    app.dependency_overrides[request_unit_of_work] = lambda: None
    try:
        yield TestClient(app), recorded
    finally:
        app.dependency_overrides.pop(auth_module.get_current_user, None)
        app.dependency_overrides.pop(auth_module.get_current_active_user, None)
        app.dependency_overrides.pop(request_unit_of_work, None)


def test_a_comment_names_alice_when_the_body_says_bob(alice):
    client, recorded = alice
    resp = client.post(
        "/api/cases/c-1/comments",
        json={"author": "bob", "content": "Closing as benign"},
    )

    assert resp.status_code == 200, resp.text
    assert recorded["comment"] == "alice"
    assert resp.json()["author"] == "alice"


def test_a_comment_without_an_author_is_accepted(alice):
    client, recorded = alice
    resp = client.post("/api/cases/c-1/comments", json={"content": "noted"})

    assert resp.status_code == 200, resp.text
    assert recorded["comment"] == "alice"


def test_evidence_names_alice_when_the_body_says_bob(alice):
    client, recorded = alice
    resp = client.post(
        "/api/v1/cases/c-1/evidence",
        json={"evidence_type": "file", "name": "pcap", "collected_by": "bob"},
    )

    assert resp.status_code == 200, resp.text
    assert recorded["evidence"] == "alice"
    assert resp.json()["collected_by"] == "alice"


def test_evidence_without_collected_by_is_accepted(alice):
    client, recorded = alice
    resp = client.post(
        "/api/cases/c-1/evidence",
        json={"evidence_type": "log", "name": "auth.log"},
    )

    assert resp.status_code == 200, resp.text
    assert recorded["evidence"] == "alice"


def test_a_custody_entry_names_alice_when_the_body_says_bob(alice):
    client, recorded = alice
    resp = client.post(
        "/api/cases/c-1/evidence/7/chain-of-custody",
        json={"action": "transferred", "user": "bob", "notes": "to the lab"},
    )

    assert resp.status_code == 200, resp.text
    assert recorded["custody"] == "alice"


def test_a_custody_entry_without_a_user_is_accepted(alice):
    client, recorded = alice
    resp = client.post(
        "/api/cases/c-1/evidence/7/chain-of-custody",
        json={"action": "analyzed"},
    )

    assert resp.status_code == 200, resp.text
    assert recorded["custody"] == "alice"


def test_a_relationship_names_alice_when_the_body_says_bob(alice):
    client, recorded = alice
    resp = client.post(
        "/api/cases/c-1/relationships",
        json={
            "related_case_id": "c-2",
            "relationship_type": "related",
            "created_by": "bob",
        },
    )

    assert resp.status_code == 200, resp.text
    assert recorded["relationship"] == "alice"
    assert resp.json()["created_by"] == "alice"


def test_a_relationship_without_created_by_is_accepted(alice):
    client, recorded = alice
    resp = client.post(
        "/api/cases/c-1/relationships",
        json={"related_case_id": "c-2", "relationship_type": "duplicate"},
    )

    assert resp.status_code == 200, resp.text
    assert recorded["relationship"] == "alice"
