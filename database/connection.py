"""
Database connection management for Vigil SOC.

Handles database connections, session management, and connection pooling.
"""

import os
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Optional, Generator, TYPE_CHECKING
from urllib.parse import parse_qsl, quote, unquote, urlsplit
from contextlib import contextmanager
from sqlalchemy import create_engine, inspect, pool, text
from sqlalchemy.orm import sessionmaker, Session
from sqlalchemy.engine import Engine

if TYPE_CHECKING:
    from services.db_proxy import ProxyConfig

from database.models import Base

# Import all models to register them with Base.metadata before create_all()
from database.models import (
    Finding,
    Case,
    SketchMapping,
    AttackLayer,
    AIDecisionLog,
    SystemConfig,
    UserPreference,
    IntegrationConfig,
    ConfigAuditLog,
    SLAPolicy,
    CaseSLA,
    CaseComment,
    CaseWatcher,
    CaseEvidence,
    CaseIOC,
    CaseTask,
    CaseTemplate,
    CaseRelationship,
    CaseMetrics,
    CaseAttachment,
    CaseClosureInfo,
    CaseEscalation,
    CaseAuditLog,
    User,
    Role,
    Investigation,
    InvestigationLog,
    LLMInteractionLog,
    SharedIOC,
    CaseNotification,
    CustomAgent,
    CustomWorkflow,
    Skill,
    LLMProviderConfig,
    Conversation,
    ChatMessage,
)

logger = logging.getLogger(__name__)


# Secrets-store keys for the platform DB proxy. Read at boot before
# the DB engine exists, so they must live in the encrypted secrets
# store (DB-independent), not SystemConfig.
_PLATFORM_DB_PROXY_KEYS = {
    "proxy_type": "PLATFORM_DB_PROXY_TYPE",
    "proxy_host": "PLATFORM_DB_PROXY_HOST",
    "proxy_port": "PLATFORM_DB_PROXY_PORT",
    "proxy_username": "PLATFORM_DB_PROXY_USERNAME",
    "proxy_password": "PLATFORM_DB_PROXY_PASSWORD",
    "ssh_private_key_path": "PLATFORM_DB_SSH_PRIVATE_KEY_PATH",
    "ssh_key_passphrase": "PLATFORM_DB_SSH_KEY_PASSPHRASE",
    "verify_proxy_tls": "PLATFORM_DB_VERIFY_PROXY_TLS",
}


def _load_platform_db_proxy() -> "ProxyConfig":
    """Read platform-DB proxy settings from the encrypted secrets store.

    Imports are local because services.db_proxy imports
    ``backend.secrets_manager`` which itself isn't part of database/'s
    boot dependency. Returns a disabled ProxyConfig when nothing is
    configured.
    """
    try:
        from services.db_proxy import ProxyConfig
        from backend.secrets_manager import get_secret
    except ImportError:
        # If the secrets manager isn't importable yet skip proxy support gracefully.
        from services.db_proxy import ProxyConfig

        return ProxyConfig()

    raw: dict[str, object] = {}
    # `attr`, not `field`: dataclasses.field is imported at module scope.
    for attr, secret_key in _PLATFORM_DB_PROXY_KEYS.items():
        value = get_secret(secret_key)
        if value is not None and value != "":
            raw[attr] = value
    if not raw or (raw.get("proxy_type") or "none").lower() in ("", "none"):
        return ProxyConfig()
    raw.setdefault("verify_proxy_tls", True)
    if isinstance(raw.get("verify_proxy_tls"), str):
        raw["verify_proxy_tls"] = raw["verify_proxy_tls"].lower() not in (
            "false",
            "0",
            "no",
            "off",
        )
    # Password / passphrase already came out of the secrets store, so
    # we don't pass *_secret_key to from_dict — values are inline.
    return ProxyConfig.from_dict(raw)


# libpq parameters we accept from a user-supplied DSN. Everything else is
# rejected: sslcert/sslkey/sslrootcert/passfile/service take *local file paths*,
# so honouring them would hand an authenticated admin a file-read/probe
# primitive against the backend host. Allowlist, not blocklist.
_ALLOWED_DSN_PARAMS = frozenset(
    {"sslmode", "connect_timeout", "application_name", "target_session_attrs"}
)
_ALLOWED_DSN_SCHEMES = frozenset({"postgresql", "postgres", "postgresql+psycopg2"})


@dataclass(frozen=True)
class ParsedDsn:
    """A validated PostgreSQL connection string."""

    host: str
    port: int
    database: str
    user: str
    password: str
    query: Dict[str, str] = field(default_factory=dict)


def parse_connection_string(dsn: str) -> ParsedDsn:
    dsn = (dsn or "").strip()
    if not dsn:
        raise ValueError("empty connection string")

    parts = urlsplit(dsn)
    if parts.scheme not in _ALLOWED_DSN_SCHEMES:
        raise ValueError(f"unsupported scheme: {parts.scheme or '(none)'}")

    try:
        port = parts.port or 5432
    except ValueError:
        raise ValueError("port must be numeric") from None

    # Decode before validating: urlsplit leaves the host percent-encoded, so a
    # raw startswith("/") check misses "%2Fvar%2Frun" — which SQLAlchemy would
    # decode straight back into a unix socket path.
    host = unquote(parts.hostname or "")
    if not host:
        raise ValueError("missing host")
    if host.startswith("/"):
        raise ValueError("unix socket paths are not supported")
    if any(c.isspace() or ord(c) < 32 for c in host):
        raise ValueError("host contains invalid characters")
    database = unquote(parts.path.lstrip("/"))
    if not database:
        raise ValueError("missing database name")

    query = {k.lower(): v for k, v in parse_qsl(parts.query, keep_blank_values=False)}
    rejected = set(query) - _ALLOWED_DSN_PARAMS
    if rejected:
        raise ValueError(
            f"unsupported connection parameter(s): {', '.join(sorted(rejected))}"
        )

    return ParsedDsn(
        host=host,
        port=port,
        database=database,
        user=unquote(parts.username or ""),
        password=unquote(parts.password or ""),
        query=query,
    )


def _load_connection_string_secret() -> Optional[str]:
    """Read POSTGRESQL_CONNECTION_STRING from the **encrypted store only**.

    Deliberately not ``get_secret()``: that falls back to the environment, and
    ``backend/main.py`` stuffs a hardcoded default connection string into
    ``os.environ`` for the MCP servers whenever the secret is unset. Reading
    through the fallback chain would let that default outrank an operator's
    POSTGRES_* variables — silently pinning them to localhost. The encrypted
    store is where Settings -> PostgreSQL writes, so it alone expresses intent.

    Local import for the same reason as :func:`_load_platform_db_proxy` —
    ``database/`` must not hard-depend on the secrets manager at import time.
    """
    try:
        from backend.secrets_manager import get_secrets_manager
    except ImportError:
        return None
    try:
        backend = get_secrets_manager().encrypted_backend
        return (
            backend.get("POSTGRESQL_CONNECTION_STRING")
            if backend.is_available()
            else None
        )
    except Exception as e:  # noqa: BLE001
        logger.debug("Could not read POSTGRESQL_CONNECTION_STRING: %s", e)
        return None


class DatabaseConfig:
    def __init__(self, *, connection_string: Optional[str] = None):
        """Initialize from the connection-string secret, else the environment."""
        dsn = (
            connection_string
            if connection_string is not None
            else _load_connection_string_secret()
        )
        self.source = "env"
        if dsn:
            try:
                self._from_dsn(parse_connection_string(dsn))
                self.source = "connection_string"
            except ValueError as e:
                logger.error(
                    "Invalid POSTGRESQL_CONNECTION_STRING (%s); using POSTGRES_* env",
                    e,
                )
                self._from_env()
        else:
            self._from_env()

        # Connection pool settings
        self.pool_size = int(os.getenv("DB_POOL_SIZE", "10"))
        self.max_overflow = int(os.getenv("DB_MAX_OVERFLOW", "20"))
        self.pool_timeout = int(os.getenv("DB_POOL_TIMEOUT", "30"))
        self.pool_recycle = int(os.getenv("DB_POOL_RECYCLE", "3600"))
        try:
            self.proxy = _load_platform_db_proxy()
        except Exception as e:  # noqa: BLE001
            # A malformed proxy secret must not make retarget unrecoverable.
            from services.db_proxy import ProxyConfig

            logger.error("Ignoring invalid platform DB proxy config: %s", e)
            self.proxy = ProxyConfig()

    def _from_env(self) -> None:
        self.host = os.getenv("POSTGRES_HOST", "localhost")
        self.port = int(os.getenv("POSTGRES_PORT", "5432"))
        self.database = os.getenv("POSTGRES_DB", "deeptempo_soc")
        self.user = os.getenv("POSTGRES_USER", "deeptempo")
        self.password = os.getenv(
            "POSTGRES_PASSWORD", "deeptempo_secure_password_change_me"
        )
        self.ssl_mode = os.getenv("POSTGRES_SSL_MODE", "prefer")
        self.extra_query: Dict[str, str] = {}

    def _from_dsn(self, parsed: ParsedDsn) -> None:
        self.host = parsed.host
        self.port = parsed.port
        self.database = parsed.database
        self.user = parsed.user
        self.password = parsed.password
        self.ssl_mode = parsed.query.get("sslmode") or os.getenv(
            "POSTGRES_SSL_MODE", "prefer"
        )
        self.extra_query = {k: v for k, v in parsed.query.items() if k != "sslmode"}

    def get_database_url(
        self,
        async_driver: bool = False,
        *,
        host: Optional[str] = None,
        port: Optional[int] = None,
    ) -> str:
        driver = "postgresql+asyncpg" if async_driver else "postgresql+psycopg2"
        effective_host = host or self.host
        effective_port = port or self.port
        user = quote(self.user, safe="")
        password = quote(self.password, safe="")
        url = (
            f"{driver}://{user}:{password}"
            f"@{effective_host}:{effective_port}/{self.database}"
        )

        params = dict(getattr(self, "extra_query", {}) or {})
        if self.ssl_mode != "prefer":
            params["sslmode"] = self.ssl_mode
        if params:
            url += "?" + "&".join(
                f"{k}={quote(v, safe='')}" for k, v in sorted(params.items())
            )

        return url


@dataclass(frozen=True)
class RetargetResult:
    """Outcome of a successful :meth:`DatabaseManager.retarget`."""

    config: "DatabaseConfig"
    in_flight_at_swap: int = 0


# Backend, LLM worker and daemon are separate processes with independent
# DatabaseManager singletons, so an API-driven retarget only moves one of them.
# Left alone, the daemon would keep ingesting into the old database while the
# backend wrote to the new one — silent divergence, the worst failure mode for
# a SOC tool. The DSN lives in the secrets file, which is file-backed and
# DB-independent (you cannot read the new database's address from the old
# database), so its mtime is the cross-process change signal.
_CONFIG_CHECK_INTERVAL = float(os.getenv("DB_CONFIG_CHECK_INTERVAL", "5"))


def db_config_generation() -> float:
    """Change-stamp for the DB config: the secrets file's mtime, else 0.0.

    Returns 0.0 when the encrypted backend isn't in use — the dotenv backend
    has no mtime tracking, so propagation is unavailable there.
    """
    try:
        from backend.secrets_manager import get_secrets_manager

        backend = get_secrets_manager().encrypted_backend
        if not backend.is_available():
            return 0.0
        return backend._current_mtime()
    except Exception:  # noqa: BLE001
        return 0.0


class DatabaseManager:
    """Manages database connections and sessions."""

    _instance: Optional["DatabaseManager"] = None
    _engine: Optional[Engine] = None
    _session_factory: Optional[sessionmaker] = None

    def __new__(cls):
        """Singleton pattern to ensure only one database manager exists."""
        if cls._instance is None:
            cls._instance = super(DatabaseManager, cls).__new__(cls)
        return cls._instance

    def __init__(self):
        """Initialize the database manager."""
        if not hasattr(self, "_initialized"):
            self.config = DatabaseConfig()
            self._proxy_handle = None
            self._initialized = True

    def _build(self, config: DatabaseConfig, echo: bool) -> tuple[Engine, Any]:
        """Resolve the proxy and create an engine. Touches no instance state.

        Keeping this side-effect free is what lets :meth:`retarget` validate a
        candidate before disturbing the live engine.
        """
        host, port, proxy = config.host, config.port, None
        if config.proxy.enabled:
            from services.db_proxy import apply as apply_proxy

            proxy = apply_proxy(host, port, config.proxy)
            host, port = proxy.host, proxy.port
            logger.info(
                "Platform DB proxy active: type=%s effective endpoint %s:%s",
                config.proxy.proxy_type,
                host,
                port,
            )

        engine = create_engine(
            config.get_database_url(host=host, port=port),
            echo=echo,
            pool_size=config.pool_size,
            max_overflow=config.max_overflow,
            pool_timeout=config.pool_timeout,
            pool_recycle=config.pool_recycle,
            pool_pre_ping=True,  # Verify connections before using them
            # Bounded so validating an unreachable host fails in seconds rather
            # than hanging a worker thread for the OS TCP timeout (~75s).
            connect_args={"connect_timeout": 5},
        )

        return engine, proxy

    def retarget(
        self,
        config: Optional[DatabaseConfig] = None,
        *,
        echo: bool = False,
        validate: bool = True,
    ):
        new_config = config or DatabaseConfig()
        engine, proxy = self._build(new_config, echo)

        if validate:
            try:
                with engine.connect() as conn:
                    conn.execute(text("SELECT 1"))
            except Exception:
                engine.dispose()
                if proxy is not None:
                    proxy.close()
                raise  # live engine/proxy untouched

        old_engine, old_proxy = self._engine, self._proxy_handle
        # Checked-out connections survive dispose() and finish against the OLD
        # database, so report how many were in flight rather than pretend not.
        in_flight = old_engine.pool.checkedout() if old_engine is not None else 0

        self.config = new_config
        self._engine = engine
        self._proxy_handle = proxy
        self._session_factory = sessionmaker(
            bind=engine,
            expire_on_commit=False,
            autoflush=True,
            autocommit=False,
        )

        if old_engine is not None:
            old_engine.dispose()
        if old_proxy is not None and old_proxy is not proxy:
            try:
                old_proxy.close()
            except Exception as e:  # noqa: BLE001
                logger.warning("Could not close previous DB proxy: %s", e)

        self._config_generation = db_config_generation()
        self._generation_checked_at = time.monotonic()

        logger.info(
            "Database target: %s:%s/%s (source=%s)",
            new_config.host,
            new_config.port,
            new_config.database,
            new_config.source,
        )
        return RetargetResult(config=new_config, in_flight_at_swap=in_flight)

    def refresh_if_stale(self) -> bool:
        """Adopt a DB config another process wrote. Returns True if we swapped.

        Rate-limited to one stat() per ``_CONFIG_CHECK_INTERVAL`` so it can sit
        on a hot path. Failures are swallowed: a process that cannot reach the
        new target must keep serving the old one rather than fall over.
        """
        if self._engine is None:
            return False
        now = time.monotonic()
        if now - getattr(self, "_generation_checked_at", 0.0) < _CONFIG_CHECK_INTERVAL:
            return False
        self._generation_checked_at = now

        generation = db_config_generation()
        if generation == 0.0 or generation == getattr(self, "_config_generation", 0.0):
            return False

        old = (self.config.host, self.config.port, self.config.database)
        try:
            new_config = DatabaseConfig()
        except Exception as e:  # noqa: BLE001
            logger.warning("Could not re-read DB config: %s", e)
            self._config_generation = generation
            return False
        if (new_config.host, new_config.port, new_config.database) == old:
            self._config_generation = generation  # secrets changed, but not ours
            return False

        try:
            self.retarget(new_config, validate=True)
        except Exception as e:  # noqa: BLE001
            logger.error(
                "DB config changed to %s:%s/%s but it is unreachable; "
                "staying on %s:%s/%s (%s)",
                new_config.host,
                new_config.port,
                new_config.database,
                *old,
                e,
            )
            self._config_generation = generation  # don't retry every interval
            return False
        logger.info("Adopted database config change from another process")
        return True

    def initialize(self, echo: bool = False, *, force: bool = False):
        """
        Initialize the database engine and session factory.

        Args:
            echo: If True, log all SQL statements
            force: Rebuild against freshly-read config even if already initialized
        """
        if self._engine is not None and not force:
            logger.warning("Database already initialized")
            return
        try:
            # validate=False: create_engine is lazy today, so cold boot has
            # always succeeded with postgres down (scripts/init_schema.py and
            # database_data_service rely on it). Only retarget() validates.
            self.retarget(None if force else self.config, echo=echo, validate=False)
        except Exception as e:
            logger.error(f"Failed to initialize database: {e}")
            raise

    def schema_report(self) -> Dict[str, Any]:
        """Classify the current target against the ORM models.

        Compares **columns**, not just table names: a name-only check calls an
        outdated schema healthy — every table exists — right until the app hits
        a column that isn't there and it surfaces as a mystery application bug.

        ``empty`` no Vigil tables (safe to provision) / ``ok`` / ``drifted``
        (tables exist, columns missing — needs scripts/migrate_schema.py, since
        create_all is checkfirst=True and won't alter them) / ``unknown``.
        """
        if self._engine is None:
            raise RuntimeError("Database not initialized. Call initialize() first.")
        try:
            inspector = inspect(self._engine)
            present = set(inspector.get_table_names())
        except Exception as e:  # noqa: BLE001
            logger.warning("Could not inspect target schema: %s", e)
            return {"state": "unknown", "missing_tables": [], "missing_columns": {}}

        expected = set(Base.metadata.tables)
        missing_tables = sorted(expected - present)
        missing_columns: Dict[str, list] = {}
        for name in sorted(expected & present):
            try:
                actual = {c["name"] for c in inspector.get_columns(name)}
            except Exception:  # noqa: BLE001
                continue
            gap = sorted({c.name for c in Base.metadata.tables[name].columns} - actual)
            if gap:
                missing_columns[name] = gap

        if not (expected & present):
            state = "empty"
        elif missing_columns or missing_tables:
            state = "drifted"
        else:
            state = "ok"
        return {
            "state": state,
            "missing_tables": missing_tables,
            "missing_columns": missing_columns,
        }

    def create_tables(self):
        """Create all database tables."""
        if self._engine is None:
            raise RuntimeError("Database not initialized. Call initialize() first.")

        try:
            Base.metadata.create_all(self._engine)
            logger.info("Database tables created successfully")
        except Exception as e:
            logger.error(f"Failed to create database tables: {e}")
            raise

    def drop_tables(self):
        """Drop all database tables. USE WITH CAUTION!"""
        if self._engine is None:
            raise RuntimeError("Database not initialized. Call initialize() first.")

        try:
            Base.metadata.drop_all(self._engine)
            logger.warning("All database tables dropped")
        except Exception as e:
            logger.error(f"Failed to drop database tables: {e}")
            raise

    def get_session(self) -> Session:
        """
        Get a new database session.

        Returns:
            SQLAlchemy session
        """
        if self._session_factory is None:
            raise RuntimeError("Database not initialized. Call initialize() first.")

        return self._session_factory()

    @contextmanager
    def session_scope(self) -> Generator[Session, None, None]:
        """
        Provide a transactional scope around a series of operations.

        Usage:
            with db_manager.session_scope() as session:
                # Use session here
                session.add(obj)
                # Automatically commits on success, rolls back on exception

        Yields:
            SQLAlchemy session
        """
        session = self.get_session()
        try:
            yield session
            session.commit()
        except Exception as e:
            session.rollback()
            logger.error(f"Database transaction failed: {e}")
            raise
        finally:
            session.close()

    def close(self):
        """Close the database connection pool."""
        if self._engine is not None:
            self._engine.dispose()
            self._engine = None
            self._session_factory = None
            logger.info("Database connection pool closed")
        if self._proxy_handle is not None:
            try:
                self._proxy_handle.close()
            except Exception as exc:  # pragma: no cover - defensive
                logger.warning("Failed to close DB proxy handle cleanly: %s", exc)
            self._proxy_handle = None

    def health_check(self) -> bool:
        """
        Check if the database is accessible.

        Returns:
            True if database is accessible, False otherwise
        """
        try:
            with self.session_scope() as session:
                session.execute(text("SELECT 1"))
            return True
        except Exception as e:
            logger.error(f"Database health check failed: {e}")
            return False

    @property
    def engine(self) -> Optional[Engine]:
        """Get the database engine."""
        return self._engine


# Global database manager instance
_db_manager: Optional[DatabaseManager] = None


def get_db_manager() -> DatabaseManager:
    """
    Get the global database manager instance.

    Returns:
        DatabaseManager instance
    """
    global _db_manager
    if _db_manager is None:
        _db_manager = DatabaseManager()
    return _db_manager


def get_db_session() -> Session:
    """
    Get a new database session.

    Returns:
        SQLAlchemy session
    """
    db_manager = get_db_manager()
    # The session entry point every process shares, so this is where the LLM
    # worker and daemon notice a retarget the backend performed. Rate-limited.
    db_manager.refresh_if_stale()
    return db_manager.get_session()


def get_db() -> Generator[Session, None, None]:
    """Generator dependency for FastAPI — always closes the session after the request.

    Use this with ``Depends(get_db)`` instead of ``Depends(get_db_session)``.
    ``get_db_session`` returns a plain Session; FastAPI only calls cleanup for
    generator dependencies, so ``Depends(get_db_session)`` leaks a connection
    on every request.
    """
    manager = get_db_manager()
    manager.refresh_if_stale()  # rate-limited; adopts another process's retarget
    session = manager.get_session()
    try:
        yield session
    finally:
        session.close()


def get_session() -> Session:
    """
    Get a database session (convenience function for imports).

    This is a convenience wrapper around get_db_session() for backward compatibility.

    Returns:
        SQLAlchemy session
    """
    return get_db_session()


def init_database(echo: bool = False, create_tables: bool = True):
    """
    Initialize the database.

    Args:
        echo: If True, log all SQL statements
        create_tables: If True, create all tables
    """
    db_manager = get_db_manager()
    db_manager.initialize(echo=echo)

    if create_tables:
        db_manager.create_tables()
