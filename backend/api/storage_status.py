"""
Storage backend status API endpoints.

Provides information about the current data storage backend (PostgreSQL or JSON).
"""

import logging
import threading

from fastapi import APIRouter, Depends, HTTPException

from backend.middleware.auth import get_current_active_user, require_settings_admin
from database.models import User

logger = logging.getLogger(__name__)

router = APIRouter()

# Serializes engine swaps; two concurrent retargets would race on the manager.
_RETARGET_LOCK = threading.Lock()


@router.get("/status")
async def get_storage_status():
    """
    Get information about the current storage backend.

    Returns:
        Dictionary with storage backend information
    """
    try:
        from services.database_data_service import DatabaseDataService
        from core.config import is_demo_mode

        test_service = DatabaseDataService()
        backend_info = test_service.get_backend_info()
        demo_mode = is_demo_mode()

        return {
            "backend": backend_info["backend"],
            "database_available": backend_info.get("database_available", False),
            "demo_mode": demo_mode,
            "description": _get_backend_description(backend_info["backend"], demo_mode),
            "recommendations": _get_recommendations(backend_info),
        }

    except Exception as e:
        logger.error(f"Error getting storage status: {e}")
        return {
            "backend": "unknown",
            "database_available": False,
            "demo_mode": False,
            "json_available": True,
            "error": str(e),
            "description": "Unable to determine storage backend",
        }


def _get_backend_description(backend: str, demo_mode: bool = False) -> str:
    """Get human-readable description of the backend."""
    if demo_mode or backend == "demo":
        return "Demo mode: Using generated sample data for demonstration"
    descriptions = {
        "postgresql": "Using PostgreSQL database for production-grade data storage",
        "json": "Using JSON file storage (development/fallback mode)",
        "unknown": "Storage backend status unknown",
    }
    return descriptions.get(backend, "Unknown storage backend")


def _get_recommendations(backend_info: dict) -> list:
    """Get recommendations based on current backend status."""
    recommendations = []

    backend = backend_info.get("backend")
    database_available = backend_info.get("database_available", False)

    if backend == "json" and not database_available:
        recommendations.append(
            {
                "title": "Enable PostgreSQL for Production",
                "description": "Currently using JSON file storage. For better performance and reliability, enable PostgreSQL.",
                "action": "Start database with: cd docker && docker compose up -d postgres",
                "priority": "medium",
            }
        )

    if backend == "postgresql":
        recommendations.append(
            {
                "title": "Database Running",
                "description": "PostgreSQL is active and ready for production use.",
                "action": "Set up automated backups for data protection",
                "priority": "low",
            }
        )

    return recommendations


@router.get("/health")
async def check_storage_health():
    """
    Perform health check on the storage backend.

    Returns:
        Health status of the storage backend
    """
    try:
        from services.database_data_service import DatabaseDataService
        from core.config import is_demo_mode

        service = DatabaseDataService()
        is_healthy = True
        demo_mode = is_demo_mode()

        # Test basic operations
        try:
            findings = service.get_findings()
            cases = service.get_cases()

            if demo_mode:
                backend = "demo"
            elif service.is_using_database():
                backend = "postgresql"
            else:
                backend = "json"

            return {
                "healthy": is_healthy,
                "backend": backend,
                "demo_mode": demo_mode,
                "findings_count": len(findings),
                "cases_count": len(cases),
                "message": "Demo mode active with sample data"
                if demo_mode
                else "Storage backend is functioning normally",
            }

        except Exception as e:
            return {
                "healthy": False,
                "backend": "unknown",
                "demo_mode": demo_mode,
                "error": str(e),
                "message": "Storage backend health check failed",
            }

    except Exception as e:
        logger.error(f"Health check error: {e}")
        return {
            "healthy": False,
            "backend": "unknown",
            "demo_mode": False,
            "error": str(e),
            "message": "Unable to perform health check",
        }


@router.post("/reconnect")
def reconnect_database(current_user: User = Depends(get_current_active_user)):
    """
    Re-read the database configuration and swap onto it, without a restart.

    Handles both "PostgreSQL came up after the app did" and "the admin changed
    the connection string in Settings" — the config is re-read every call.

    The new engine is built and probed *before* the old one is discarded, so a
    bad target leaves the working connection serving. Sync def on purpose: it
    blocks on I/O, so FastAPI runs it in a threadpool instead of stalling the
    event loop.
    """
    require_settings_admin(current_user)

    from database.connection import get_db_manager

    with _RETARGET_LOCK:
        db_manager = get_db_manager()
        previous = getattr(db_manager, "config", None)
        try:
            result = db_manager.retarget(validate=True)
        except Exception as e:
            logger.error("Database retarget rejected: %s", e)
            return {
                "success": False,
                "changed": False,
                "backend": "postgresql" if db_manager.health_check() else "json",
                "message": f"New target rejected; the existing connection is intact: {e}",
                "database_available": db_manager.health_check(),
                "recommendation": "Check the connection string, and that the database is reachable.",
            }

        cfg = result.config
        schema = db_manager.schema_report()
        _audit_retarget(previous, cfg, current_user)
        logger.info(
            "Reconnected to %s:%s/%s (schema=%s)",
            cfg.host,
            cfg.port,
            cfg.database,
            schema["state"],
        )
        if schema["state"] == "drifted":
            logger.warning(
                "Target schema is out of date: missing tables=%s columns=%s",
                schema["missing_tables"],
                schema["missing_columns"],
            )
        return {
            "success": True,
            "changed": True,
            "backend": "postgresql",
            "message": _schema_message(schema),
            "database_available": True,
            "connection_info": {
                "host": cfg.host,
                "port": cfg.port,
                "database": cfg.database,
                "source": cfg.source,
            },
            # Connections checked out at swap time finish against the previous
            # database — surface that rather than let it look atomic.
            "in_flight_connections_at_swap": result.in_flight_at_swap,
            "schema_state": schema["state"],
            "missing_tables": schema["missing_tables"],
            "missing_columns": schema["missing_columns"],
            "requires_schema_init": schema["state"] == "empty",
            "requires_migration": schema["state"] == "drifted",
            # The worker and daemon are separate processes with their own
            # engines; they converge on their next config check.
            "other_processes_stale": True,
        }


def _schema_message(schema: dict) -> str:
    state = schema["state"]
    if state == "empty":
        return "Connected. The database has no Vigil tables yet - initialize the schema to use it."
    if state == "drifted":
        return (
            "Connected, but the database schema is out of date and the app will "
            "misbehave against it. Run scripts/migrate_schema.py to bring it up to date."
        )
    if state == "unknown":
        return "Connected, but the schema could not be inspected."
    return "Connected to PostgreSQL database"


def _audit_retarget(previous, current, user: User) -> None:
    """Record the target change. Best-effort: never fails the swap."""
    try:
        from database.connection import get_db_manager
        from database.models import ConfigAuditLog

        before = (
            f"{previous.host}:{previous.port}/{previous.database}"
            if previous
            else "(none)"
        )
        # session_scope() commits on exit; a bare Session used as a context
        # manager only closes, so the audit row would be silently dropped.
        with get_db_manager().session_scope() as session:
            session.add(
                ConfigAuditLog(
                    config_key="DATABASE_TARGET",
                    action="update",
                    old_value=before,
                    new_value=f"{current.host}:{current.port}/{current.database}",
                    changed_by=str(user.user_id),
                )
            )
    except Exception as e:  # noqa: BLE001
        logger.warning("Could not write retarget audit log: %s", e)


@router.post("/init-schema")
def init_schema(current_user: User = Depends(get_current_active_user)):
    """
    Provision Vigil's tables into an **empty** target database.

    Split out of /reconnect, and deliberately narrow. ``create_all`` is
    ``checkfirst=True``: it creates what's missing and silently skips tables
    that already exist, whatever shape they're in. That makes it correct for an
    empty database and useless — actively misleading — for a drifted one.

    So this refuses anything but ``state == "empty"``:
      - ``drifted`` -> scripts/migrate_schema.py owns that (it has the real
        ALTER TABLE migrations); create_all here would report success and
        change nothing.
      - ``ok`` -> nothing to do.

    Refusing on non-empty also means a mistyped host can't scatter ~50 tables
    through an unrelated production database.
    """
    require_settings_admin(current_user)

    from database.connection import get_db_manager

    db_manager = get_db_manager()
    before = db_manager.schema_report()
    if before["state"] == "drifted":
        raise HTTPException(
            status_code=409,
            detail=(
                "Database has an out-of-date Vigil schema. create_all cannot alter "
                "existing tables - run scripts/migrate_schema.py against it instead. "
                f"Missing columns: {before['missing_columns']}"
            ),
        )
    if before["state"] == "ok":
        return {"success": True, "message": "Schema already up to date", **before}
    if before["state"] == "unknown":
        raise HTTPException(status_code=409, detail="Cannot inspect the target schema.")

    try:
        db_manager.create_tables()
    except Exception as e:
        logger.error("Schema init failed: %s", e)
        raise HTTPException(status_code=500, detail=f"Schema init failed: {e}")
    logger.info("Provisioned Vigil schema into empty database")
    return {"success": True, "message": "Schema created", **db_manager.schema_report()}


@router.post("/switch-backend")
async def switch_backend(backend: str):
    """
    Attempt to switch storage backend (requires restart).

    Args:
        backend: Target backend ('database' or 'json')

    Returns:
        Status of the switch request
    """
    if backend not in ["database", "json"]:
        return {
            "success": False,
            "message": 'Invalid backend. Must be "database" or "json"',
        }

    import os
    from pathlib import Path

    try:
        # Update .env file
        env_file = Path(".env")

        if env_file.exists():
            with open(env_file, "r") as f:
                lines = f.readlines()

            updated = False
            with open(env_file, "w") as f:
                for line in lines:
                    if line.startswith("DATA_BACKEND="):
                        f.write(f"DATA_BACKEND={backend}\n")
                        updated = True
                    else:
                        f.write(line)

                if not updated:
                    f.write(f"\n# Data storage backend\nDATA_BACKEND={backend}\n")

            return {
                "success": True,
                "message": f"Backend set to {backend}. Please restart the application for changes to take effect.",
                "requires_restart": True,
            }
        else:
            return {
                "success": False,
                "message": ".env file not found. Please create one from env.example",
            }

    except Exception as e:
        logger.error(f"Error switching backend: {e}")
        return {"success": False, "message": f"Failed to switch backend: {str(e)}"}
