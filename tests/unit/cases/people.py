"""A throwaway users table for the assignee checks (#3155).

alice is an active analyst. bob is deactivated, medic-bot is a service
account. ``carol`` is one account's username and another's email, which
folds to two rows.
"""

from sqlalchemy import create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from core.storage.models import Principal, Role, User
from core.storage.models.base import Base


@compiles(JSONB, "sqlite")
def _jsonb_is_json_on_sqlite(type_, compiler, **kw):
    return "JSON"


def _account(session, username, email, *, active=True, service=False):
    session.add(
        Principal(
            principal_id=username,
            kind="service_account" if service else "person",
            label=username,
        )
    )
    session.add(
        User(
            user_id=username,
            principal_id=username,
            username=username,
            email=email,
            password_hash="",
            full_name=f"{username.title()} Example",
            role_id="role-analyst",
            is_active=active,
            is_verified=True,
            service_account=service,
            login_count=0,
            failed_login_count=0,
        )
    )


def people() -> Session:
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(
        engine, tables=[Role.__table__, Principal.__table__, User.__table__]
    )
    session = sessionmaker(bind=engine)()
    session.add(
        Role(
            role_id="role-analyst",
            name="Analyst",
            description="",
            permissions={},
            is_system_role=True,
        )
    )
    _account(session, "alice", "alice@example.com")
    _account(session, "soc-lead", "lead@example.com")
    _account(session, "bob", "bob@example.com", active=False)
    _account(session, "medic-bot", "medic@example.com", service=True)
    _account(session, "carol", "carol@example.com")
    _account(session, "dave", "Carol")
    session.commit()
    return session
