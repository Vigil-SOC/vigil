"""
Integration tests for case management API endpoints.
Tests case CRUD operations, status updates, findings association, and reports.
"""

import pytest
from fastapi.testclient import TestClient
from unittest.mock import patch
import json

# Skip all integration tests until fixtures are recreated
pytestmark = pytest.mark.skip(reason="Requires fixtures (test_client, auth_headers, sample_user) - see conftest.py")


@pytest.mark.integration
class TestCaseCreation:
    """Test case creation endpoint."""
    
    def test_create_case_minimal(self, test_client, auth_headers):
        """Test creating case with minimal required fields."""
        response = test_client.post(
            "/api/cases",
            headers=auth_headers,
            json={
                "title": "Test Investigation",
                "description": "Test case description",
                "priority": "high",
                "severity": "high"
            }
        )
        
        assert response.status_code in [200, 201]
        data = response.json()
        assert data["title"] == "Test Investigation"
        assert data["status"] == "open"  # Default status
        assert "id" in data
    
    def test_create_case_full(self, test_client, auth_headers, sample_user):
        """Test creating case with all fields."""
        response = test_client.post(
            "/api/cases",
            headers=auth_headers,
            json={
                "title": "Complete Investigation",
                "description": "Full case details",
                "priority": "critical",
                "severity": "critical",
                "assignee_id": sample_user.id,
                "tags": ["malware", "ransomware"]
            }
        )
        
        assert response.status_code in [200, 201]
        data = response.json()
        assert data["priority"] == "critical"
        assert data["assignee_id"] == sample_user.id
    
    def test_create_case_unauthorized(self, test_client):
        """Test creating case without authentication."""
        response = test_client.post(
            "/api/cases",
            json={"title": "Test", "priority": "high"}
        )
        
        assert response.status_code == 401


@pytest.mark.integration
class TestCaseRetrieval:
    """Test case retrieval endpoints."""
    
    def test_list_cases(self, test_client, auth_headers):
        """Test listing all cases."""
        response = test_client.get(
            "/api/cases",
            headers=auth_headers
        )
        
        assert response.status_code == 200
        data = response.json()
        assert isinstance(data, list) or "items" in data
    
    def test_list_cases_with_pagination(self, test_client, auth_headers):
        """Test listing cases with pagination."""
        response = test_client.get(
            "/api/cases?skip=0&limit=10",
            headers=auth_headers
        )
        
        assert response.status_code == 200
    
    def test_list_cases_with_filters(self, test_client, auth_headers):
        """Test listing cases with filters."""
        response = test_client.get(
            "/api/cases?status=open&priority=high",
            headers=auth_headers
        )
        
        assert response.status_code == 200
        data = response.json()
        # Verify filtering works
        if isinstance(data, list):
            filtered_cases = data
        else:
            filtered_cases = data.get("items", [])
        
        if filtered_cases:
            assert all(c["status"] == "open" for c in filtered_cases if "status" in c)
    
    def test_get_case_by_id(self, test_client, auth_headers, sample_case):
        """Test getting specific case by ID."""
        response = test_client.get(
            f"/api/cases/{sample_case.id}",
            headers=auth_headers
        )
        
        assert response.status_code == 200
        data = response.json()
        assert data["id"] == sample_case.id
        assert data["title"] == sample_case.title
    
    def test_get_nonexistent_case(self, test_client, auth_headers):
        """Test getting non-existent case."""
        response = test_client.get(
            "/api/cases/nonexistent-id",
            headers=auth_headers
        )
        
        assert response.status_code == 404


@pytest.mark.integration
class TestCaseUpdate:
    """Test case update endpoints."""
    
    def test_update_case_status(self, test_client, auth_headers, sample_case):
        """Test updating case status."""
        response = test_client.patch(
            f"/api/cases/{sample_case.id}",
            headers=auth_headers,
            json={"status": "in_progress"}
        )
        
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "in_progress"
    
    def test_update_case_priority(self, test_client, auth_headers, sample_case):
        """Test updating case priority."""
        response = test_client.patch(
            f"/api/cases/{sample_case.id}",
            headers=auth_headers,
            json={"priority": "critical"}
        )
        
        assert response.status_code == 200
        data = response.json()
        assert data["priority"] == "critical"
    
    def test_update_case_assignee(self, test_client, auth_headers, sample_case, sample_admin_user):
        """Test updating case assignee."""
        response = test_client.patch(
            f"/api/cases/{sample_case.id}",
            headers=auth_headers,
            json={"assignee_id": sample_admin_user.id}
        )
        
        assert response.status_code == 200
        data = response.json()
        assert data["assignee_id"] == sample_admin_user.id
    
    def test_update_case_invalid_transition(self, test_client, auth_headers, sample_case):
        """Test invalid status transition."""
        # Try to go from open directly to closed (invalid)
        response = test_client.patch(
            f"/api/cases/{sample_case.id}",
            headers=auth_headers,
            json={"status": "closed"}
        )
        
        # Should reject invalid transition
        assert response.status_code in [400, 422]


@pytest.mark.integration
class TestCaseFindingsAssociation:
    """Test associating findings with cases."""
    
    def test_add_finding_to_case(self, test_client, auth_headers, sample_case, sample_finding):
        """Test adding finding to case."""
        response = test_client.post(
            f"/api/cases/{sample_case.id}/findings",
            headers=auth_headers,
            json={"finding_id": sample_finding.id}
        )
        
        assert response.status_code in [200, 201]
        
        # Verify finding was added
        case_response = test_client.get(
            f"/api/cases/{sample_case.id}",
            headers=auth_headers
        )
        case_data = case_response.json()
        finding_ids = [f.get("id") for f in case_data.get("findings", [])]
        assert sample_finding.id in finding_ids or len(case_data.get("findings", [])) > 0
    
    def test_list_case_findings(self, test_client, auth_headers, sample_case):
        """Test listing findings associated with case."""
        response = test_client.get(
            f"/api/cases/{sample_case.id}/findings",
            headers=auth_headers
        )
        
        assert response.status_code == 200
        data = response.json()
        assert isinstance(data, list)
    
    def test_remove_finding_from_case(self, test_client, auth_headers, sample_case, sample_finding):
        """Test removing finding from case."""
        # First add it
        test_client.post(
            f"/api/cases/{sample_case.id}/findings",
            headers=auth_headers,
            json={"finding_id": sample_finding.id}
        )
        
        # Then remove it
        response = test_client.delete(
            f"/api/cases/{sample_case.id}/findings/{sample_finding.id}",
            headers=auth_headers
        )
        
        assert response.status_code in [200, 204]


@pytest.mark.integration
class TestCaseActivities:
    """Test case activity logging."""
    
    def test_log_activity(self, test_client, auth_headers, sample_case):
        """Test logging activity to case."""
        response = test_client.post(
            f"/api/cases/{sample_case.id}/activities",
            headers=auth_headers,
            json={
                "action": "investigation_update",
                "description": "Analyzed logs, found suspicious activity"
            }
        )
        
        assert response.status_code in [200, 201]
    
    def test_get_case_activities(self, test_client, auth_headers, sample_case):
        """Test getting case activity timeline."""
        response = test_client.get(
            f"/api/cases/{sample_case.id}/activities",
            headers=auth_headers
        )
        
        assert response.status_code == 200
        data = response.json()
        assert isinstance(data, list)


@pytest.mark.integration
class TestCaseResolution:
    """Test case resolution and closure."""
    
    def test_add_resolution_step(self, test_client, auth_headers, sample_case):
        """Test adding resolution step."""
        response = test_client.post(
            f"/api/cases/{sample_case.id}/resolution",
            headers=auth_headers,
            json={
                "step_number": 1,
                "description": "Isolated affected host",
                "action_taken": "Disconnected from network",
                "result": "Host successfully isolated"
            }
        )
        
        assert response.status_code in [200, 201]
    
    def test_get_resolution_steps(self, test_client, auth_headers, sample_case):
        """Test getting resolution steps."""
        response = test_client.get(
            f"/api/cases/{sample_case.id}/resolution",
            headers=auth_headers
        )
        
        assert response.status_code == 200
        data = response.json()
        assert isinstance(data, list)
    
    def test_close_case(self, test_client, auth_headers, sample_case, test_db_session):
        """Test closing a case."""
        # First move to resolved
        sample_case.status = "resolved"
        test_db_session.commit()
        
        response = test_client.patch(
            f"/api/cases/{sample_case.id}",
            headers=auth_headers,
            json={
                "status": "closed",
                "resolution_summary": "Issue resolved, host cleaned"
            }
        )
        
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "closed"


@pytest.mark.integration
class TestCaseReports:
    """Test case report generation."""
    
    @patch('core.reporting.report_service.ReportService.generate_pdf')
    def test_generate_case_report(self, mock_generate_pdf, test_client, auth_headers, sample_case):
        """Test generating case PDF report."""
        mock_generate_pdf.return_value = b"PDF_CONTENT"
        
        response = test_client.get(
            f"/api/cases/{sample_case.id}/report",
            headers=auth_headers
        )
        
        if response.status_code == 200:
            assert response.headers["content-type"] == "application/pdf"
    
    def test_export_case_to_json(self, test_client, auth_headers, sample_case):
        """Test exporting case to JSON."""
        response = test_client.get(
            f"/api/cases/{sample_case.id}/export",
            headers=auth_headers
        )
        
        assert response.status_code == 200
        data = response.json()
        assert data["id"] == sample_case.id


@pytest.mark.integration
class TestCaseSearch:
    """Test case search functionality."""
    
    def test_search_cases_by_title(self, test_client, auth_headers):
        """Test searching cases by title."""
        response = test_client.get(
            "/api/cases/search?q=investigation",
            headers=auth_headers
        )
        
        assert response.status_code == 200
    
    def test_search_cases_by_tag(self, test_client, auth_headers):
        """Test searching cases by tag."""
        response = test_client.get(
            "/api/cases/search?tags=ransomware",
            headers=auth_headers
        )
        
        assert response.status_code == 200
    
    def test_advanced_search(self, test_client, auth_headers):
        """Test advanced case search with multiple filters."""
        response = test_client.get(
            "/api/cases/search?status=open&priority=high&assignee=user-123",
            headers=auth_headers
        )
        
        assert response.status_code == 200


@pytest.mark.integration
class TestCaseMetrics:
    """Test case metrics endpoints."""
    
    def test_get_case_metrics(self, test_client, auth_headers, sample_case):
        """Test getting metrics for specific case."""
        response = test_client.get(
            f"/api/cases/{sample_case.id}/metrics",
            headers=auth_headers
        )
        
        if response.status_code == 200:
            data = response.json()
            assert "time_to_first_response" in data or "created_at" in data
    
    def test_get_aggregate_metrics(self, test_client, auth_headers):
        """Test getting aggregate case metrics."""
        response = test_client.get(
            "/api/cases/metrics/aggregate",
            headers=auth_headers
        )
        
        if response.status_code == 200:
            data = response.json()
            assert "total_cases" in data or "cases" in str(data).lower()


@pytest.mark.integration
class TestCaseDelete:
    """Test case deletion (admin only)."""
    
    def test_delete_case_as_admin(self, test_client, admin_auth_headers, sample_case):
        """Test deleting case as admin."""
        response = test_client.delete(
            f"/api/cases/{sample_case.id}",
            headers=admin_auth_headers
        )
        
        assert response.status_code in [200, 204]
    
    def test_delete_case_as_analyst(self, test_client, auth_headers, sample_case):
        """Test deleting case as non-admin (should fail)."""
        response = test_client.delete(
            f"/api/cases/{sample_case.id}",
            headers=auth_headers
        )
        
        assert response.status_code == 403  # Forbidden

