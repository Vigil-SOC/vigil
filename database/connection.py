"""
Database connection management for Vigil SOC.

Handles database connections, session management, and connection pooling.
"""

import os
import logging
from typing import Optional, Generator
from contextlib import contextmanager
from sqlalchemy import create_engine, event, pool, text
from sqlalchemy.orm import sessionmaker, Session
from sqlalchemy.engine import Engine

from database.models import Base

# Import all models to register them with Base.metadata before create_all()
from database.models import (
    Finding, Case, SketchMapping, AttackLayer, AIDecisionLog,
    SystemConfig, UserPreference, IntegrationConfig, ConfigAuditLog,
    SLAPolicy, CaseSLA, CaseComment, CaseWatcher, CaseEvidence, CaseIOC,
    CaseTask, CaseTemplate, CaseRelationship, CaseMetrics, CaseAttachment,
    CaseClosureInfo, CaseEscalation, CaseAuditLog,
    User, Role, Investigation, InvestigationLog, SharedIOC, CaseNotification,
)

logger = logging.getLogger(__name__)


class DatabaseConfig:
    """Database configuration management."""
    
    def __init__(self):
        """Initialize database configuration from environment variables."""
        self.host = os.getenv('POSTGRES_HOST', 'localhost')
        self.port = int(os.getenv('POSTGRES_PORT', '5432'))
        self.database = os.getenv('POSTGRES_DB', 'deeptempo_soc')
        self.user = os.getenv('POSTGRES_USER', 'deeptempo')
        self.password = os.getenv('POSTGRES_PASSWORD', 'deeptempo_secure_password_change_me')
        
        # Connection pool settings
        self.pool_size = int(os.getenv('DB_POOL_SIZE', '5'))
        self.max_overflow = int(os.getenv('DB_MAX_OVERFLOW', '10'))
        self.pool_timeout = int(os.getenv('DB_POOL_TIMEOUT', '30'))
        self.pool_recycle = int(os.getenv('DB_POOL_RECYCLE', '3600'))
        
        # SSL settings
        self.ssl_mode = os.getenv('POSTGRES_SSL_MODE', 'prefer')
    
    def get_database_url(self, async_driver: bool = False) -> str:
        """
        Get the database connection URL.
        
        Args:
            async_driver: If True, use async driver (asyncpg), otherwise use psycopg2
        
        Returns:
            Database connection URL
        """
        driver = 'postgresql+asyncpg' if async_driver else 'postgresql+psycopg2'
        
        url = f"{driver}://{self.user}:{self.password}@{self.host}:{self.port}/{self.database}"
        
        if self.ssl_mode != 'prefer':
            url += f"?sslmode={self.ssl_mode}"
        
        return url


class DatabaseManager:
    """Manages database connections and sessions."""
    
    _instance: Optional['DatabaseManager'] = None
    _engine: Optional[Engine] = None
    _session_factory: Optional[sessionmaker] = None
    
    def __new__(cls):
        """Singleton pattern to ensure only one database manager exists."""
        if cls._instance is None:
            cls._instance = super(DatabaseManager, cls).__new__(cls)
        return cls._instance
    
    def __init__(self):
        """Initialize the database manager."""
        if not hasattr(self, '_initialized'):
            self.config = DatabaseConfig()
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
            database_url = self.config.get_database_url()
            
            self._engine = create_engine(
                database_url,
                echo=echo,
                pool_size=self.config.pool_size,
                max_overflow=self.config.max_overflow,
                pool_timeout=self.config.pool_timeout,
                pool_recycle=self.config.pool_recycle,
                pool_pre_ping=True,  # Verify connections before using them
            )

            # Instrument SQLAlchemy with OTEL tracing
            try:
                from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor
                SQLAlchemyInstrumentor().instrument(engine=self._engine)
                logger.debug("SQLAlchemy OTEL instrumentation enabled")
            except Exception as _inst_err:
                logger.debug("SQLAlchemy OTEL instrumentation skipped: %s", _inst_err)

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
                autocommit=False
            )
            
            logger.info(f"Database initialized: {self.config.host}:{self.config.port}/{self.config.database}")
        
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

