"""
Unit tests verifying that all SQLAlchemy models are registered with
Base.metadata before create_all() is called.

Importing database.connection is sufficient to trigger model registration
because the module-level imports at the top of connection.py pull in every
model class defined in database/models.py.
"""

import pytest


def test_integration_configs_table_registered():
    """integration_configs must be in Base.metadata after importing database.connection."""
    import database.connection  # noqa: F401 — side-effect import registers models
    from database.models import Base

    assert "integration_configs" in Base.metadata.tables, (
        "integration_configs table is not registered with Base.metadata. "
        "Ensure IntegrationConfig is imported in database/connection.py before create_all()."
    )


def test_config_audit_log_table_registered():
    """config_audit_log must be in Base.metadata after importing database.connection."""
    import database.connection  # noqa: F401 — side-effect import registers models
    from database.models import Base

    assert "config_audit_log" in Base.metadata.tables, (
        "config_audit_log table is not registered with Base.metadata. "
        "Ensure ConfigAuditLog is imported in database/connection.py before create_all()."
    )


def test_all_model_tables_registered():
    """All Base subclasses defined in database/models must appear in Base.metadata.tables."""
    import database.connection  # noqa: F401 — side-effect import registers models
    import database.models as models_module
    from database.models import Base
    from sqlalchemy.orm import DeclarativeBase

    # Collect every concrete model class (direct subclasses of Base)
    registered_tables = set(Base.metadata.tables.keys())

    for name in dir(models_module):
        obj = getattr(models_module, name)
        if (
            isinstance(obj, type)
            and issubclass(obj, Base)
            and obj is not Base
            and hasattr(obj, "__tablename__")
        ):
            assert obj.__tablename__ in registered_tables, (
                f"Model '{name}' with __tablename__='{obj.__tablename__}' is not registered "
                "in Base.metadata. Add it to the import block in database/connection.py."
            )
