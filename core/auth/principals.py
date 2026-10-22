"""Principals: the one stable id for whoever or whatever acts.

A person or a service account is the principal whose id is its ``user_id``.
An agent is ``agent:<key>``: each built-in ``AgentId``, each custom agent, and
each agent-service plugin. The system is ``system``. Agents are never user
rows. A principal is never deleted or reused; deleting its account or agent
sets ``deleted_at``.
"""

from __future__ import annotations

from typing import Optional

from sqlalchemy.orm import Session

from core.storage.models import Principal, User
from core.time import utcnow

PERSON = "person"
SERVICE_ACCOUNT = "service_account"
AGENT = "agent"
SYSTEM = "system"

SYSTEM_PRINCIPAL_ID = "system"
# The agent-service plugins (services/agent/plugins/*/plugin.yaml): a set of
# their own, not AgentId. 0003_principals_sign_ins.sql seeds the same list.
PLUGIN_IDS = ("hunt", "investigate", "rootcause", "adjudicate", "compose", "tally")
CHAT_PLUGIN_ID = "chat"
CHAT_LABEL = "the chat assistant"


def agent_principal_id(key: str) -> str:
    return f"agent:{key}"


def add_for_user(session: Session, user: User) -> Principal:
    """The principal for a new account, flushed before the user row that
    refers to it. Call before ``session.add(user)``."""
    principal = Principal(
        principal_id=user.user_id,
        kind=SERVICE_ACCOUNT if user.service_account is True else PERSON,
        label=user.username,
    )
    session.add(principal)
    session.flush()
    user.principal_id = principal.principal_id
    return principal


def ensure_agent(session: Session, key: str, label: Optional[str]) -> Principal:
    """``agent:<key>``, created, or brought back if a deleted agent's id is reused."""
    principal = session.get(Principal, agent_principal_id(key))
    if principal is None:
        principal = Principal(
            principal_id=agent_principal_id(key), kind=AGENT, label=label
        )
        session.add(principal)
    else:
        principal.deleted_at = None
        principal.label = label
    return principal


def mark_deleted(session: Session, principal_id: str) -> None:
    principal = session.get(Principal, principal_id)
    if principal is not None and principal.deleted_at is None:
        principal.deleted_at = utcnow()
