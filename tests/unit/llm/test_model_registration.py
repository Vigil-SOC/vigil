"""
Unit tests verifying that all SQLAlchemy models are registered with
Base.metadata before create_all() is called.

Importing core.storage.connection is sufficient to trigger model registration
because it imports core.storage.models, whose package init loads every domain
submodule so all mapped classes register on Base.metadata.
"""

import importlib
import pkgutil


def test_integration_configs_table_registered():
    """integration_configs must be in Base.metadata after importing core.storage.connection."""
    import core.storage.connection  # noqa: F401 — side-effect import registers models
    from core.storage.models import Base

    assert "integration_configs" in Base.metadata.tables, (
        "integration_configs table is not registered with Base.metadata. "
        "Import IntegrationConfig from core/storage/models/__init__.py."
    )


def test_config_audit_log_table_registered():
    """config_audit_log must be in Base.metadata after importing core.storage.connection."""
    import core.storage.connection  # noqa: F401 — side-effect import registers models
    from core.storage.models import Base

    assert "config_audit_log" in Base.metadata.tables, (
        "config_audit_log table is not registered with Base.metadata. "
        "Import ConfigAuditLog from core/storage/models/__init__.py."
    )


def test_all_model_tables_registered():
    """All Base subclasses re-exported from core.storage.models appear in metadata."""
    import core.storage.connection  # noqa: F401 — side-effect import registers models
    import core.storage.models as models_module
    from core.storage.models import Base

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
                "in Base.metadata. Import its submodule from core/storage/models/__init__.py."
            )


def test_every_models_submodule_is_imported():
    """A mapped class left in an unimported submodule never appears in dir(models)."""
    import core.storage.connection  # noqa: F401 — side-effect import registers models
    import core.storage.models as models_pkg
    from core.storage.models import Base

    registered = {mapper.class_ for mapper in Base.registry.mappers}
    for module_info in pkgutil.iter_modules(models_pkg.__path__):
        module = importlib.import_module(f"{models_pkg.__name__}.{module_info.name}")
        for name in dir(module):
            obj = getattr(module, name)
            if (
                isinstance(obj, type)
                and issubclass(obj, Base)
                and obj is not Base
                and hasattr(obj, "__tablename__")
            ):
                assert obj in registered, (
                    f"{module_info.name}.{name} is not registered on Base.metadata. "
                    "Import its submodule from core/storage/models/__init__.py."
                )
