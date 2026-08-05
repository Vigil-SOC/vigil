"""Guard the mutation-tracking fix for issue #543.

`obj.col.append(...)` on a plain `JSONB` column gives SQLAlchemy no way to see
the change: the attribute never becomes dirty, no UPDATE is emitted, and the
append is silently discarded on commit. That is not a hypothetical — case
auto-assignment (`services/case_workflow_service.py:403`) and escalation
(`:470`) were losing their timeline entries exactly this way.

The fix declares the appendable collection columns with
`MutableList.as_mutable(JSONB)`. These tests assert the mechanism rather than
the symptom, so they need no database and run in the main unit gate. The
end-to-end proof (append → commit → re-read in a fresh session) lives in
`tests/integration/test_case_timeline_persistence.py`, which needs Postgres.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "backend"))

os.environ.setdefault("JWT_SECRET_KEY", "test-only-secret-not-for-prod")

pytestmark = pytest.mark.unit


#: Columns appended to in place today, plus the rest of the same family on the
#: same models. Each entry is (model attribute path, why it must be tracked).
TRACKED_COLLECTIONS = [
    ("Case", "timeline", "case_workflow_service appends lifecycle events in place"),
    ("Case", "activities", "backend/api/cases.py appends the merge activity in place"),
    ("Case", "notes", "same family; the next .append() must be safe by default"),
    ("Case", "resolution_steps", "same family"),
    ("CaseEvidence", "chain_of_custody", "a lost append is a hole in an audit trail"),
]


@pytest.mark.parametrize("model_name,attr,reason", TRACKED_COLLECTIONS)
def test_collection_column_is_mutation_tracked(model_name, attr, reason):
    """Assigning a plain list must coerce to a change-tracking MutableList.

    Coercion on assignment is exactly what a plain JSONB column does not do, so
    this fails before the fix and is the precondition for an in-place append
    ever being persisted. ``reason`` documents why each column is in scope.
    """
    from sqlalchemy.ext.mutable import MutableList

    import database.models as models

    model = getattr(models, model_name)
    obj = model()
    setattr(obj, attr, [{"seed": True}])

    value = getattr(obj, attr)
    assert isinstance(value, MutableList), (
        f"{model_name}.{attr} is not mutation-tracked, so an in-place append "
        f"would be silently discarded ({reason}). Declare it with "
        f"MutableList.as_mutable(JSONB) — see issue #543."
    )


@pytest.mark.parametrize("model_name,attr,reason", TRACKED_COLLECTIONS)
def test_in_place_append_emits_a_change_event(model_name, attr, reason):
    """An in-place append must notify SQLAlchemy's mutable-extension listeners.

    This is the behaviour the fix buys: `MutableList.changed()` fires, which is
    what marks the parent attribute dirty so an UPDATE is emitted.
    """
    import database.models as models

    model = getattr(models, model_name)
    obj = model()
    setattr(obj, attr, [])

    collection = getattr(obj, attr)
    fired = []
    # `changed()` is the hook the extension calls to propagate a mutation up to
    # the owning attribute. Spying on it proves the append is observable without
    # needing a session or a database.
    original_changed = collection.changed
    collection.changed = lambda *a, **kw: (fired.append(True), original_changed())[1]

    collection.append({"appended": True})

    assert fired, (
        f"appending to {model_name}.{attr} did not signal a change; the append "
        f"would be lost on commit ({reason})."
    )


def test_no_collection_column_uses_a_shared_mutable_default():
    """`default=[]` is one list object shared by every instance.

    SQLAlchemy treats a non-callable default as a scalar constant, so every row
    inserted without an explicit value gets the *same* list. `default=list` is a
    factory and gives each row its own. The codebase already used `default=list`
    in 13 places and `default=[]` in 8 — this pins the correct form so the two
    conventions cannot drift back apart.
    """
    source = (REPO / "database" / "models.py").read_text()
    assert "default=[]" not in source, (
        "database/models.py contains `default=[]`, a shared mutable default. "
        "Use `default=list` so each row gets its own collection."
    )
    assert "default={}" not in source, (
        "database/models.py contains `default={}`, a shared mutable default. "
        "Use `default=dict`."
    )
