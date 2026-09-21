"""Deleting an SLA policy answers, rather than crashing after it has answered.

`case_slas.sla_policy_id` is a foreign key with no `ON DELETE` clause, so the
database refuses to drop a policy any case still references. The handler did
not `flush()`, so that refusal arrived at commit -- after the handler had
returned -- and `core/routing.py:142`'s function-scoped session turns it into a
bare 500. The operator was told nothing, having just been told by the 400 to
pass `force=true`.

The count cannot be trusted to predict it either: `in_use` is read under the
request's own scope while the foreign key is global, so a policy can read as
unused and still be referenced. The last test is that case.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException
from sqlalchemy.exc import IntegrityError

REPO = Path(__file__).resolve().parents[3]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

pytestmark = pytest.mark.unit

POLICY_ID = "sla-high"


def _session(*, in_use: int, flush_raises: bool = False) -> MagicMock:
    """A session that answers the policy lookup and the usage count separately."""
    from core.storage.models import CaseSLA, SLAPolicy

    policy = MagicMock(spec=SLAPolicy)
    policy.policy_id = POLICY_ID

    def query(model):
        chain = MagicMock()
        if model is SLAPolicy:
            chain.filter.return_value.first.return_value = policy
        elif model is CaseSLA:
            chain.filter.return_value.count.return_value = in_use
        return chain

    session = MagicMock()
    session.query.side_effect = query
    if flush_raises:
        session.flush.side_effect = IntegrityError(
            "DELETE FROM sla_policies", {}, Exception("violates foreign key constraint")
        )
    return session


@pytest.mark.asyncio
async def test_an_unused_policy_is_deleted():
    from core.cases import sla_policies_router as router

    session = _session(in_use=0)

    result = await router.delete_sla_policy(POLICY_ID, session=session)

    assert result["success"] is True
    session.delete.assert_called_once()


@pytest.mark.asyncio
async def test_a_missing_policy_is_a_404():
    from core.cases import sla_policies_router as router

    session = MagicMock()
    session.query.return_value.filter.return_value.first.return_value = None

    with pytest.raises(HTTPException) as exc:
        await router.delete_sla_policy(POLICY_ID, session=session)

    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_a_policy_in_use_is_refused_and_says_how_many():
    from core.cases import sla_policies_router as router

    session = _session(in_use=3)

    with pytest.raises(HTTPException) as exc:
        await router.delete_sla_policy(POLICY_ID, session=session)

    assert exc.value.status_code == 409
    assert "3" in exc.value.detail
    session.delete.assert_not_called()


@pytest.mark.asyncio
async def test_the_refusal_does_not_promise_an_escape_that_cannot_work():
    """`force=true` could never delete a referenced policy; saying so was the bug."""
    from core.cases import sla_policies_router as router

    session = _session(in_use=3)

    with pytest.raises(HTTPException) as exc:
        await router.delete_sla_policy(POLICY_ID, session=session)

    assert "force" not in exc.value.detail.lower()
    assert "deactivat" in exc.value.detail.lower()


@pytest.mark.asyncio
async def test_a_reference_the_count_could_not_see_is_still_an_answer():
    """The count is request-scoped; the constraint is global. Flush, then explain."""
    from core.cases import sla_policies_router as router

    session = _session(in_use=0, flush_raises=True)

    with pytest.raises(HTTPException) as exc:
        await router.delete_sla_policy(POLICY_ID, session=session)

    assert exc.value.status_code == 409
    assert "deactivat" in exc.value.detail.lower()
