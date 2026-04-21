"""
Unit tests for database models.
Tests model creation, validation, relationships, and queries.
Also validates that all models are registered with Base.metadata so their
tables are created on startup via create_all().
"""

import pytest
from datetime import datetime, timedelta
from sqlalchemy.exc import IntegrityError

# Marker applied to classes that require a live database session.
_requires_db = pytest.mark.skip(reason="Requires database fixtures - see tests/conftest.py to set up")

try:
    from deeptempo_core.database.models import (
        User, Case, Finding,
        SLAPolicy
    )
except ImportError:
    User = Case = Finding = SLAPolicy = None


class TestDatabaseMetadataRegistration:
    """Validate that all SQLAlchemy models are registered with Base.metadata.

    These tests do NOT require a live database connection; they only check
    that importing ``database.connection`` causes all model classes to be
    registered with ``Base.metadata`` (so that ``create_all()`` creates
    every expected table on a fresh install).
    """

    def test_integration_configs_in_metadata(self):
        """integration_configs table must be present in Base.metadata.tables."""
        # Importing database.connection triggers all model imports, which
        # registers every model class with Base.metadata.
        from database.models import Base
        import database.connection  # noqa: F401 - side-effect import

        assert "integration_configs" in Base.metadata.tables, (
            "integration_configs table is missing from Base.metadata — "
            "IntegrationConfig was not imported before create_all()"
        )

    def test_config_audit_log_in_metadata(self):
        """config_audit_log table must be present in Base.metadata.tables."""
        from database.models import Base
        import database.connection  # noqa: F401

        assert "config_audit_log" in Base.metadata.tables, (
            "config_audit_log table is missing from Base.metadata — "
            "ConfigAuditLog was not imported before create_all()"
        )

    def test_all_model_tables_in_metadata(self):
        """Every model subclass defined in database.models must appear in Base.metadata.tables."""
        from database.models import Base
        import database.connection  # noqa: F401

        expected_tables = {
            "findings",
            "cases",
            "sketch_mappings",
            "attack_layers",
            "ai_decision_logs",
            "system_config",
            "user_preferences",
            "integration_configs",
            "config_audit_log",
            "sla_policies",
            "case_slas",
            "case_comments",
            "case_watchers",
            "case_evidence",
            "case_iocs",
            "case_tasks",
            "case_templates",
            "case_relationships",
            "case_metrics",
            "case_attachments",
            "case_closure_info",
            "case_escalations",
            "case_audit_logs",
            "users",
            "roles",
            "investigations",
            "investigation_logs",
            "llm_interaction_logs",
            "shared_iocs",
            "case_notifications",
            "custom_agents",
            "custom_workflows",
            "skills",
        }

        registered = set(Base.metadata.tables.keys())
        missing = expected_tables - registered

        assert not missing, (
            f"The following tables are missing from Base.metadata and will not be "
            f"created by create_all() on a fresh install: {sorted(missing)}"
        )


@_requires_db
class TestUserModel:
    """Test User model."""

    def test_create_user(self, db_session):
        """Test creating a user."""
        from database.models import User

        user = User(
            username="analyst@company.com",
            email="analyst@company.com",
            hashed_password="hashed_pw",
            role="analyst",
        )

        db_session.add(user)
        db_session.commit()

        assert user.id is not None
        assert user.username == "analyst@company.com"

    def test_user_unique_email(self, db_session):
        """Test email uniqueness constraint."""
        from database.models import User

        user1 = User(
            username="user1",
            email="same@company.com",
            hashed_password="hash1",
            role="analyst",
        )
        user2 = User(
            username="user2",
            email="same@company.com",
            hashed_password="hash2",
            role="analyst",
        )

        db_session.add(user1)
        db_session.commit()

        db_session.add(user2)
        with pytest.raises(IntegrityError):
            db_session.commit()

    def test_user_roles(self, db_session):
        """Test user role assignment."""
        from database.models import User

        analyst = User(
            username="analyst",
            email="analyst@company.com",
            hashed_password="hash",
            role="analyst",
        )
        admin = User(
            username="admin",
            email="admin@company.com",
            hashed_password="hash",
            role="admin",
        )

        db_session.add_all([analyst, admin])
        db_session.commit()

        assert analyst.role == "analyst"
        assert admin.role == "admin"

    def test_user_timestamp(self, db_session):
        """Test automatic timestamp creation."""
        from database.models import User

        user = User(
            username="test",
            email="test@company.com",
            hashed_password="hash",
            role="analyst",
        )

        db_session.add(user)
        db_session.commit()

        assert user.created_at is not None
        assert isinstance(user.created_at, datetime)


@_requires_db
class TestCaseModel:
    """Test Case model."""

    def test_create_case(self, db_session):
        """Test creating a case."""
        from database.models import Case

        case = Case(
            title="Security Incident",
            description="Suspicious activity detected",
            priority="high",
            status="open",
            severity="high",
        )

        db_session.add(case)
        db_session.commit()

        assert case.id is not None
        assert case.title == "Security Incident"

    def test_case_priority_levels(self, db_session):
        """Test case priority levels."""
        from database.models import Case

        priorities = ["critical", "high", "medium", "low", "informational"]

        for priority in priorities:
            case = Case(
                title=f"Case {priority}",
                priority=priority,
                status="open",
                severity=priority,
            )
            db_session.add(case)

        db_session.commit()

        cases = db_session.query(Case).all()
        assert len(cases) == 5

    def test_case_status_transitions(self, db_session):
        """Test case status transitions."""
        from database.models import Case

        case = Case(
            title="Test Case",
            priority="medium",
            status="open",
            severity="medium",
        )

        db_session.add(case)
        db_session.commit()

        case.status = "investigating"
        db_session.commit()
        assert case.status == "investigating"

        case.status = "resolved"
        db_session.commit()
        assert case.status == "resolved"

        case.status = "closed"
        case.closed_at = datetime.utcnow()
        db_session.commit()
        assert case.status == "closed"
        assert case.closed_at is not None

    def test_case_assignee_relationship(self, db_session):
        """Test case-assignee relationship."""
        from database.models import Case, User

        user = User(
            username="analyst",
            email="analyst@company.com",
            hashed_password="hash",
            role="analyst",
        )
        case = Case(
            title="Assigned Case",
            priority="high",
            status="investigating",
            severity="high",
            assignee=user,
        )

        db_session.add_all([user, case])
        db_session.commit()

        assert case.assignee_id == user.id
        assert case.assignee.username == "analyst"


@_requires_db
class TestFindingModel:
    """Test Finding model."""

    def test_create_finding(self, db_session):
        """Test creating a finding."""
        from database.models import Finding

        finding = Finding(
            title="Suspicious Login",
            description="Multiple failed attempts",
            severity="high",
            source="splunk",
            raw_data={"attempts": 10},
        )

        db_session.add(finding)
        db_session.commit()

        assert finding.id is not None
        assert finding.title == "Suspicious Login"

    def test_finding_case_relationship(self, db_session):
        """Test finding-case relationship."""
        from database.models import Case, Finding

        case = Case(
            title="Incident",
            priority="high",
            status="open",
            severity="high",
        )
        finding = Finding(
            title="Alert 1",
            severity="high",
            source="splunk",
            case=case,
        )

        db_session.add_all([case, finding])
        db_session.commit()

        assert finding.case_id == case.id
        assert len(case.findings) == 1
        assert case.findings[0].title == "Alert 1"

    def test_finding_iocs(self, db_session):
        """Test storing IOCs in finding."""
        from database.models import Finding

        finding = Finding(
            title="Malicious Traffic",
            severity="critical",
            source="crowdstrike",
            iocs={
                "ips": ["185.220.101.5"],
                "domains": ["evil.com"],
                "hashes": ["abc123"],
            },
        )

        db_session.add(finding)
        db_session.commit()

        assert "ips" in finding.iocs
        assert "185.220.101.5" in finding.iocs["ips"]

    def test_finding_mitre_mapping(self, db_session):
        """Test MITRE ATT&CK mapping."""
        from database.models import Finding

        finding = Finding(
            title="Lateral Movement",
            severity="high",
            source="defender",
            mitre_techniques=["T1021", "T1078"],
        )

        db_session.add(finding)
        db_session.commit()

        assert "T1021" in finding.mitre_techniques


@_requires_db
class TestSLAPolicyModel:
    """Test SLAPolicy model."""

    def test_create_sla_policy(self, db_session):
        """Test creating SLA policy."""
        from database.models import SLAPolicy

        policy = SLAPolicy(
            name="Critical Incidents",
            priority="critical",
            response_time_minutes=15,
            resolution_time_minutes=240,
            enabled=True,
        )

        db_session.add(policy)
        db_session.commit()

        assert policy.id is not None
        assert policy.response_time_minutes == 15

    def test_sla_policy_by_priority(self, db_session):
        """Test SLA policies for different priorities."""
        from database.models import SLAPolicy

        policies = [
            SLAPolicy(name="Critical", priority="critical", response_time_minutes=15, resolution_time_minutes=240),
            SLAPolicy(name="High", priority="high", response_time_minutes=60, resolution_time_minutes=480),
            SLAPolicy(name="Medium", priority="medium", response_time_minutes=240, resolution_time_minutes=1440),
        ]

        db_session.add_all(policies)
        db_session.commit()

        critical_policy = db_session.query(SLAPolicy).filter_by(priority="critical").first()
        assert critical_policy.response_time_minutes == 15


@_requires_db
class TestModelRelationships:
    """Test model relationships."""

    def test_case_findings_relationship(self, db_session):
        """Test one-to-many case-findings relationship."""
        from database.models import Case, Finding

        case = Case(
            title="Incident",
            priority="high",
            status="open",
            severity="high",
        )
        finding1 = Finding(title="Alert 1", severity="high", source="splunk", case=case)
        finding2 = Finding(title="Alert 2", severity="medium", source="defender", case=case)

        db_session.add_all([case, finding1, finding2])
        db_session.commit()

        assert len(case.findings) == 2
        assert finding1.case_id == case.id
        assert finding2.case_id == case.id

    def test_cascade_delete(self, db_session):
        """Test cascade delete of related records."""
        from database.models import Case, Finding

        case = Case(
            title="Test Case",
            priority="low",
            status="open",
            severity="low",
        )
        finding = Finding(title="Alert", severity="low", source="test", case=case)

        db_session.add_all([case, finding])
        db_session.commit()

        case_id = case.id

        db_session.delete(case)
        db_session.commit()

        remaining_findings = db_session.query(Finding).filter_by(case_id=case_id).all()

        assert len(remaining_findings) == 0


@_requires_db
class TestModelQueries:
    """Test common model queries."""

    def test_query_open_cases(self, db_session):
        """Test querying open cases."""
        from database.models import Case

        cases = [
            Case(title="Case 1", priority="high", status="open", severity="high"),
            Case(title="Case 2", priority="medium", status="investigating", severity="medium"),
            Case(title="Case 3", priority="low", status="closed", severity="low"),
        ]

        db_session.add_all(cases)
        db_session.commit()

        open_cases = (
            db_session.query(Case)
            .filter(Case.status.in_(["open", "investigating"]))
            .all()
        )

        assert len(open_cases) == 2

    def test_query_high_priority_cases(self, db_session):
        """Test querying high priority cases."""
        from database.models import Case

        cases = [
            Case(title="Case 1", priority="critical", status="open", severity="critical"),
            Case(title="Case 2", priority="high", status="open", severity="high"),
            Case(title="Case 3", priority="medium", status="open", severity="medium"),
        ]

        db_session.add_all(cases)
        db_session.commit()

        high_priority = (
            db_session.query(Case)
            .filter(Case.priority.in_(["critical", "high"]))
            .all()
        )

        assert len(high_priority) == 2

    def test_query_cases_with_sla_breach(self, db_session):
        """Test querying cases with SLA breach."""
        from database.models import Case

        old_case = Case(
            title="Old Case",
            priority="critical",
            status="open",
            severity="critical",
            created_at=datetime.utcnow() - timedelta(hours=5),
        )
        new_case = Case(
            title="New Case",
            priority="critical",
            status="open",
            severity="critical",
            created_at=datetime.utcnow() - timedelta(minutes=10),
        )

        db_session.add_all([old_case, new_case])
        db_session.commit()

        threshold = datetime.utcnow() - timedelta(hours=4)
        breached = (
            db_session.query(Case)
            .filter(Case.created_at < threshold)
            .filter(Case.status != "closed")
            .all()
        )

        assert len(breached) == 1
        assert breached[0].title == "Old Case"
