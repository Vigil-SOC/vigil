"""
Shared test fixtures and configuration for pytest.
This file provides common fixtures used across unit and integration tests.
"""

import os
import sys
from datetime import datetime, timedelta
from typing import AsyncGenerator, Generator
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

# Add project root to path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

# NOTE: Imports commented out to prevent DB connection on collection
# These need to be updated to match current project structure
# from deeptempo_core.database.models import Base, User, Finding, Case, Role
# from services.api.main import app

# Placeholder imports to prevent errors
Base = None
User = None
Finding = None
Case = None
Role = None
app = None


# ============================================================================
# Database Fixtures
# ============================================================================

@pytest.fixture(scope="function")
def test_db_engine():
    """Create an in-memory SQLite database engine for testing."""
    pytest.skip("Fixture disabled - needs rewrite for current DB structure")
    # engine = create_engine(
    #     "sqlite:///:memory:",
    #     connect_args={"check_same_thread": False},
    #     poolclass=StaticPool,
    # )
    # Base.metadata.create_all(bind=engine)
    # yield engine
    # Base.metadata.drop_all(bind=engine)
    # engine.dispose()


@pytest.fixture(scope="function")
def test_db_session(test_db_engine) -> Generator[Session, None, None]:
    """Create a database session for testing."""
    pytest.skip("Fixture disabled - needs rewrite for current DB structure")
    # TestingSessionLocal = sessionmaker(
    #     autocommit=False, autoflush=False, bind=test_db_engine
    # )
    # session = TestingSessionLocal()
    # try:
    #     yield session
    # finally:
    #     session.rollback()
    #     session.close()


# ============================================================================
# API Client Fixtures
# ============================================================================

@pytest.fixture(scope="function")
def test_client(test_db_session) -> TestClient:
    """Create a test client for FastAPI application."""
    pytest.skip("Fixture disabled - needs rewrite for current app structure")
    # def override_get_db():
    #     try:
    #         yield test_db_session
    #     finally:
    #         pass
    # 
    # # Override database dependency
    # from core.storage.connection import get_db
    # app.dependency_overrides[get_db] = override_get_db
    # 
    # client = TestClient(app)
    # yield client
    # 
    # # Clear overrides
    # app.dependency_overrides.clear()


# ============================================================================
# Authentication Fixtures
# ============================================================================

@pytest.fixture
def sample_role(test_db_session) -> Role:
    """Create a sample role for testing."""
    role = Role(
        id="analyst-role-123",
        name="analyst",
        permissions={
            "cases:read": True,
            "cases:write": True,
            "findings:read": True,
            "findings:write": False,
        }
    )
    test_db_session.add(role)
    test_db_session.commit()
    test_db_session.refresh(role)
    return role


@pytest.fixture
def sample_admin_role(test_db_session) -> Role:
    """Create a sample admin role for testing."""
    role = Role(
        id="admin-role-123",
        name="admin",
        permissions={
            "cases:read": True,
            "cases:write": True,
            "cases:delete": True,
            "findings:read": True,
            "findings:write": True,
            "findings:delete": True,
            "users:read": True,
            "users:write": True,
            "users:delete": True,
        }
    )
    test_db_session.add(role)
    test_db_session.commit()
    test_db_session.refresh(role)
    return role


@pytest.fixture
def sample_user(test_db_session, sample_role) -> User:
    """Create a sample user for testing."""
    from core.auth.auth_service import AuthService
    
    user = User(
        id="user-123",
        username="testuser",
        email="test@example.com",
        password_hash=AuthService.hash_password("testpassword123"),
        role_id=sample_role.id,
        is_active=True,
        created_at=datetime.utcnow(),
    )
    test_db_session.add(user)
    test_db_session.commit()
    test_db_session.refresh(user)
    return user


@pytest.fixture
def sample_admin_user(test_db_session, sample_admin_role) -> User:
    """Create a sample admin user for testing."""
    from core.auth.auth_service import AuthService
    
    user = User(
        id="admin-123",
        username="admin",
        email="admin@example.com",
        password_hash=AuthService.hash_password("adminpassword123"),
        role_id=sample_admin_role.id,
        is_active=True,
        created_at=datetime.utcnow(),
    )
    test_db_session.add(user)
    test_db_session.commit()
    test_db_session.refresh(user)
    return user


@pytest.fixture
def auth_token(sample_user) -> str:
    """Generate a valid JWT token for testing."""
    from core.auth.auth_service import AuthService
    
    token = AuthService.create_access_token(
        data={"sub": sample_user.id, "role": sample_user.role_id}
    )
    return token


@pytest.fixture
def admin_auth_token(sample_admin_user) -> str:
    """Generate a valid admin JWT token for testing."""
    from core.auth.auth_service import AuthService
    
    token = AuthService.create_access_token(
        data={"sub": sample_admin_user.id, "role": sample_admin_user.role_id}
    )
    return token


@pytest.fixture
def auth_headers(auth_token) -> dict:
    """Create authorization headers with valid token."""
    return {"Authorization": f"Bearer {auth_token}"}


@pytest.fixture
def admin_auth_headers(admin_auth_token) -> dict:
    """Create authorization headers with admin token."""
    return {"Authorization": f"Bearer {admin_auth_token}"}


# ============================================================================
# Sample Data Fixtures
# ============================================================================

@pytest.fixture
def sample_finding(test_db_session) -> Finding:
    """Create a sample finding for testing."""
    finding = Finding(
        id="finding-123",
        title="Suspicious Network Traffic",
        description="Unusual outbound connection detected",
        severity="high",
        source="splunk",
        external_id="splunk-evt-001",
        raw_data={"src_ip": "10.0.1.5", "dst_ip": "185.220.101.5", "port": 443},
        timestamp=datetime.utcnow(),
        created_at=datetime.utcnow(),
        mitre_techniques=["T1071.001", "T1573.001"],
    )
    test_db_session.add(finding)
    test_db_session.commit()
    test_db_session.refresh(finding)
    return finding


@pytest.fixture
def sample_case(test_db_session, sample_user) -> Case:
    """Create a sample case for testing."""
    case = Case(
        id="case-123",
        title="Investigation: Suspicious Network Activity",
        description="Investigating unusual network connections from workstation",
        status="open",
        priority="high",
        severity="high",
        assignee_id=sample_user.id,
        created_at=datetime.utcnow(),
        updated_at=datetime.utcnow(),
    )
    test_db_session.add(case)
    test_db_session.commit()
    test_db_session.refresh(case)
    return case


@pytest.fixture
def multiple_findings(test_db_session) -> list[Finding]:
    """Create multiple findings for testing."""
    findings = []
    severities = ["low", "medium", "high", "critical"]
    sources = ["splunk", "crowdstrike", "azure_sentinel"]
    
    for i in range(10):
        finding = Finding(
            id=f"finding-{i}",
            title=f"Security Finding {i}",
            description=f"Test finding {i}",
            severity=severities[i % len(severities)],
            source=sources[i % len(sources)],
            external_id=f"ext-{i}",
            timestamp=datetime.utcnow() - timedelta(hours=i),
            created_at=datetime.utcnow(),
        )
        test_db_session.add(finding)
        findings.append(finding)
    
    test_db_session.commit()
    return findings


# ============================================================================
# Mock Service Fixtures
# ============================================================================

@pytest.fixture
def mock_claude_response():
    """Mock Claude API response."""
    return {
        "id": "msg_123",
        "type": "message",
        "role": "assistant",
        "content": [
            {
                "type": "text",
                "text": "This finding indicates potential C2 communication. The IP address 185.220.101.5 is associated with known malicious infrastructure."
            }
        ],
        "model": "claude-sonnet-4-20250514",
        "stop_reason": "end_turn",
        "usage": {"input_tokens": 150, "output_tokens": 50}
    }


@pytest.fixture
def mock_splunk_events():
    """Mock Splunk search results."""
    return [
        {
            "_time": "2026-01-27T10:00:00Z",
            "src_ip": "10.0.1.5",
            "dest_ip": "185.220.101.5",
            "dest_port": 443,
            "action": "allowed",
            "signature": "Suspicious Outbound Connection",
            "severity": "high",
        },
        {
            "_time": "2026-01-27T10:05:00Z",
            "src_ip": "10.0.1.5",
            "dest_ip": "185.220.101.5",
            "dest_port": 443,
            "action": "allowed",
            "signature": "Suspicious Outbound Connection",
            "severity": "high",
        }
    ]


@pytest.fixture
def mock_jira_response():
    """Mock JIRA issue creation response."""
    return {
        "id": "10001",
        "key": "SEC-123",
        "self": "https://company.atlassian.net/rest/api/2/issue/10001"
    }


@pytest.fixture
def mock_virustotal_response():
    """Mock VirusTotal API response."""
    return {
        "data": {
            "attributes": {
                "last_analysis_stats": {
                    "malicious": 15,
                    "suspicious": 2,
                    "undetected": 50,
                    "harmless": 5
                },
                "reputation": -50
            }
        }
    }


# ============================================================================
# Environment Fixtures
# ============================================================================

@pytest.fixture(autouse=True)
def mock_env_variables(monkeypatch):
    """Mock environment variables for testing."""
    monkeypatch.setenv("DATABASE_URL", "sqlite:///:memory:")
    monkeypatch.setenv("SECRET_KEY", "test-secret-key-123")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-claude-key")
    monkeypatch.setenv("SPLUNK_URL", "https://test-splunk.example.com")
    monkeypatch.setenv("TESTING", "true")


# ============================================================================
# Utility Fixtures
# ============================================================================

@pytest.fixture
def freeze_time():
    """Fixture to freeze time for testing."""
    from freezegun import freeze_time as _freeze_time
    return _freeze_time


@pytest.fixture
def faker_instance():
    """Provide a Faker instance for generating test data."""
    from faker import Faker
    return Faker()


# ============================================================================
# Cleanup
# ============================================================================

@pytest.fixture(autouse=True)
def reset_cache():
    """Reset any caches between tests."""
    yield
    # Add cache clearing logic here if needed

