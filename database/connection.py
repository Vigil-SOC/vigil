"""
Database connection management for Vigil SOC.

Handles database connections, session management, and connection pooling.
"""

import os
import logging
from typing import Optional, Generator, TYPE_CHECKING
from contextlib import contextmanager
from sqlalchemy import create_engine, event, pool, text
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
        # If the secrets manager isn't importable yet (e.g. during a
        # pre-install sanity check), skip proxy support gracefully.
        from services.db_proxy import ProxyConfig

        return ProxyConfig()

    raw: dict[str, object] = {}
    for field, secret_key in _PLATFORM_DB_PROXY_KEYS.items():
        value = get_secret(secret_key)
        if value is not None and value != "":
            raw[field] = value
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


class DatabaseConfig:
    """Database configuration management."""

    def __init__(self):
        """Initialize database configuration from environment variables."""
        self.host = os.getenv("POSTGRES_HOST", "localhost")
        self.port = int(os.getenv("POSTGRES_PORT", "5432"))
        self.database = os.getenv("POSTGRES_DB", "deeptempo_soc")
        self.user = os.getenv("POSTGRES_USER", "deeptempo")
        self.password = os.getenv(
            "POSTGRES_PASSWORD", "deeptempo_secure_password_change_me"
        )

        # Connection pool settings
        self.pool_size = int(os.getenv("DB_POOL_SIZE", "5"))
        self.max_overflow = int(os.getenv("DB_MAX_OVERFLOW", "10"))
        self.pool_timeout = int(os.getenv("DB_POOL_TIMEOUT", "30"))
        self.pool_recycle = int(os.getenv("DB_POOL_RECYCLE", "3600"))

        # SSL settings
        self.ssl_mode = os.getenv("POSTGRES_SSL_MODE", "prefer")

        # Optional proxy in front of the DB (PgBouncer or SSH tunnel).
        # Loaded lazily so unrelated tests that import DatabaseConfig
        # don't need a live secrets manager.
        self.proxy = _load_platform_db_proxy()

    def get_database_url(
        self,
        async_driver: bool = False,
        *,
        host: Optional[str] = None,
        port: Optional[int] = None,
    ) -> str:
        """
        Get the database connection URL.

        Args:
            async_driver: If True, use async driver (asyncpg), otherwise use psycopg2
            host: Override host (used after a proxy rewrites the endpoint).
            port: Override port (used after a proxy rewrites the endpoint).

        Returns:
            Database connection URL
        """
        driver = "postgresql+asyncpg" if async_driver else "postgresql+psycopg2"
        effective_host = host or self.host
        effective_port = port or self.port
        url = (
            f"{driver}://{self.user}:{self.password}"
            f"@{effective_host}:{effective_port}/{self.database}"
        )

        if self.ssl_mode != "prefer":
            url += f"?sslmode={self.ssl_mode}"

        return url


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
            # Holds an SSH tunnel (or other proxy artifact) for the
            # process lifetime when the platform DB is fronted by a
            # tunneling proxy. Closed in :meth:`close`.
            self._proxy_handle = None
            self._initialized = True

    def initialize(self, echo: bool = False):
        """
        Initialize the database engine and session factory.

        Args:
            echo: If True, log all SQL statements
        """
        if self._engine is not None:
            logger.warning("Database already initialized")
            return

        try:
            host = self.config.host
            port = self.config.port
            if self.config.proxy.enabled:
                from services.db_proxy import apply as apply_proxy

                applied = apply_proxy(host, port, self.config.proxy)
                self._proxy_handle = applied
                host = applied.host
                port = applied.port
                logger.info(
                    "Platform DB proxy active: type=%s effective endpoint %s:%s",
                    self.config.proxy.proxy_type,
                    host,
                    port,
                )

            database_url = self.config.get_database_url(host=host, port=port)

            self._engine = create_engine(
                database_url,
                echo=echo,
                pool_size=self.config.pool_size,
                max_overflow=self.config.max_overflow,
                pool_timeout=self.config.pool_timeout,
                pool_recycle=self.config.pool_recycle,
                pool_pre_ping=True,  # Verify connections before using them
            )

            # Set up event listeners
            @event.listens_for(self._engine, "connect")
            def receive_connect(dbapi_conn, connection_record):
                """Called when a new connection is created."""
                logger.debug("New database connection established")

            @event.listens_for(self._engine, "close")
            def receive_close(dbapi_conn, connection_record):
                """Called when a connection is closed."""
                logger.debug("Database connection closed")

            # Create session factory
            self._session_factory = sessionmaker(
                bind=self._engine,
                expire_on_commit=False,
                autoflush=True,
                autocommit=False,
            )

            logger.info(
                f"Database initialized: {self.config.host}:{self.config.port}/{self.config.database}"
            )

        except Exception as e:
            logger.error(f"Failed to initialize database: {e}")
            raise

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

    This is a convenience function for dependency injection in FastAPI.

    Returns:
        SQLAlchemy session
    """
    db_manager = get_db_manager()
    return db_manager.get_session()


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
