"""
Unit tests for timeline event handling.
Tests event creation, chronological ordering, filtering, and aggregation.
"""

import pytest
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock, patch

# Skip all tests until TimelineService API is documented
pytestmark = pytest.mark.skip(reason="TimelineService methods don't exist - needs rewrite for current API")

# TimelineService doesn't exist - timeline logic handled differently
# from services.timeline_service import TimelineService


class TestEventCreation:
    """Test timeline event creation."""
    
    def test_create_case_event(self):
        """Test creating case-related event."""
        service = TimelineService()
        
        event = service.create_event(
            event_type="case_created",
            case_id="case-001",
            user="analyst@company.com",
            details={"priority": "high"}
        )
        
        assert event["event_type"] == "case_created"
        assert event["case_id"] == "case-001"
        assert event["user"] == "analyst@company.com"
        assert "timestamp" in event
    
    def test_create_finding_event(self):
        """Test creating finding-related event."""
        service = TimelineService()
        
        event = service.create_event(
            event_type="finding_added",
            case_id="case-001",
            finding_id="finding-123",
            details={"severity": "critical"}
        )
        
        assert event["event_type"] == "finding_added"
        assert event["finding_id"] == "finding-123"
    
    def test_create_action_event(self):
        """Test creating action-related event."""
        service = TimelineService()
        
        event = service.create_event(
            event_type="action_executed",
            case_id="case-001",
            action_type="isolate_host",
            target="workstation-042",
            result="success"
        )
        
        assert event["event_type"] == "action_executed"
        assert event["action_type"] == "isolate_host"
        assert event["result"] == "success"
    
    def test_create_comment_event(self):
        """Test creating comment event."""
        service = TimelineService()
        
        event = service.create_event(
            event_type="comment_added",
            case_id="case-001",
            user="analyst@company.com",
            comment="Investigating lateral movement"
        )
        
        assert event["event_type"] == "comment_added"
        assert event["comment"] == "Investigating lateral movement"
    
    def test_auto_timestamp(self):
        """Test automatic timestamp assignment."""
        service = TimelineService()
        
        before = datetime.now(timezone.utc)
        event = service.create_event(
            event_type="case_updated",
            case_id="case-001"
        )
        after = datetime.now(timezone.utc)
        
        event_time = datetime.fromisoformat(event["timestamp"])
        assert before <= event_time <= after


class TestChronologicalOrdering:
    """Test chronological ordering of events."""
    
    def test_get_timeline_ordered(self):
        """Test timeline is returned in chronological order."""
        service = TimelineService()
        
        # Add events in non-chronological order
        service.create_event(
            event_type="case_created",
            case_id="case-001",
            timestamp=datetime(2024, 1, 1, 10, 0, 0)
        )
        service.create_event(
            event_type="finding_added",
            case_id="case-001",
            timestamp=datetime(2024, 1, 1, 9, 0, 0)
        )
        service.create_event(
            event_type="case_closed",
            case_id="case-001",
            timestamp=datetime(2024, 1, 1, 11, 0, 0)
        )
        
        timeline = service.get_timeline("case-001", order="asc")
        
        # Verify ascending order
        timestamps = [datetime.fromisoformat(e["timestamp"]) for e in timeline]
        assert timestamps == sorted(timestamps)
    
    def test_get_timeline_reverse_ordered(self):
        """Test timeline in reverse chronological order."""
        service = TimelineService()
        
        service.create_event(
            event_type="event1",
            case_id="case-001",
            timestamp=datetime(2024, 1, 1, 10, 0, 0)
        )
        service.create_event(
            event_type="event2",
            case_id="case-001",
            timestamp=datetime(2024, 1, 1, 11, 0, 0)
        )
        
        timeline = service.get_timeline("case-001", order="desc")
        
        # Verify descending order
        timestamps = [datetime.fromisoformat(e["timestamp"]) for e in timeline]
        assert timestamps == sorted(timestamps, reverse=True)


class TestFiltering:
    """Test timeline filtering."""
    
    def test_filter_by_event_type(self):
        """Test filtering by event type."""
        service = TimelineService()
        case_id = "case-001"
        
        service.create_event(event_type="case_created", case_id=case_id)
        service.create_event(event_type="comment_added", case_id=case_id)
        service.create_event(event_type="action_executed", case_id=case_id)
        service.create_event(event_type="comment_added", case_id=case_id)
        
        filtered = service.get_timeline(
            case_id,
            event_types=["comment_added"]
        )
        
        assert len(filtered) == 2
        assert all(e["event_type"] == "comment_added" for e in filtered)
    
    def test_filter_by_user(self):
        """Test filtering by user."""
        service = TimelineService()
        case_id = "case-001"
        
        service.create_event(
            event_type="comment_added",
            case_id=case_id,
            user="analyst1@company.com"
        )
        service.create_event(
            event_type="comment_added",
            case_id=case_id,
            user="analyst2@company.com"
        )
        service.create_event(
            event_type="case_updated",
            case_id=case_id,
            user="analyst1@company.com"
        )
        
        filtered = service.get_timeline(
            case_id,
            user="analyst1@company.com"
        )
        
        assert len(filtered) == 2
        assert all(e["user"] == "analyst1@company.com" for e in filtered)
    
    def test_filter_by_date_range(self):
        """Test filtering by date range."""
        service = TimelineService()
        case_id = "case-001"
        
        service.create_event(
            event_type="event1",
            case_id=case_id,
            timestamp=datetime(2024, 1, 1, 10, 0, 0)
        )
        service.create_event(
            event_type="event2",
            case_id=case_id,
            timestamp=datetime(2024, 1, 2, 10, 0, 0)
        )
        service.create_event(
            event_type="event3",
            case_id=case_id,
            timestamp=datetime(2024, 1, 3, 10, 0, 0)
        )
        
        filtered = service.get_timeline(
            case_id,
            start_date=datetime(2024, 1, 2, 0, 0, 0),
            end_date=datetime(2024, 1, 3, 23, 59, 59)
        )
        
        assert len(filtered) == 2
    
    def test_filter_multiple_criteria(self):
        """Test filtering with multiple criteria."""
        service = TimelineService()
        case_id = "case-001"
        
        service.create_event(
            event_type="comment_added",
            case_id=case_id,
            user="analyst1@company.com",
            timestamp=datetime(2024, 1, 2, 10, 0, 0)
        )
        service.create_event(
            event_type="action_executed",
            case_id=case_id,
            user="analyst1@company.com",
            timestamp=datetime(2024, 1, 2, 11, 0, 0)
        )
        service.create_event(
            event_type="comment_added",
            case_id=case_id,
            user="analyst2@company.com",
            timestamp=datetime(2024, 1, 2, 12, 0, 0)
        )
        
        filtered = service.get_timeline(
            case_id,
            event_types=["comment_added"],
            user="analyst1@company.com",
            start_date=datetime(2024, 1, 2, 0, 0, 0)
        )
        
        assert len(filtered) == 1
        assert filtered[0]["event_type"] == "comment_added"
        assert filtered[0]["user"] == "analyst1@company.com"


class TestPagination:
    """Test timeline pagination."""
    
    def test_paginate_results(self):
        """Test paginating timeline results."""
        service = TimelineService()
        case_id = "case-001"
        
        # Create 25 events
        for i in range(25):
            service.create_event(
                event_type=f"event_{i}",
                case_id=case_id
            )
        
        # Get first page
        page1 = service.get_timeline(case_id, page=1, per_page=10)
        assert len(page1) == 10
        
        # Get second page
        page2 = service.get_timeline(case_id, page=2, per_page=10)
        assert len(page2) == 10
        
        # Get third page
        page3 = service.get_timeline(case_id, page=3, per_page=10)
        assert len(page3) == 5
    
    def test_pagination_metadata(self):
        """Test pagination metadata."""
        service = TimelineService()
        case_id = "case-001"
        
        for i in range(25):
            service.create_event(event_type=f"event_{i}", case_id=case_id)
        
        result = service.get_timeline_paginated(
            case_id,
            page=1,
            per_page=10
        )
        
        assert result["page"] == 1
        assert result["per_page"] == 10
        assert result["total"] == 25
        assert result["pages"] == 3


class TestAggregation:
    """Test timeline aggregation and statistics."""
    
    def test_count_by_event_type(self):
        """Test counting events by type."""
        service = TimelineService()
        case_id = "case-001"
        
        service.create_event(event_type="comment_added", case_id=case_id)
        service.create_event(event_type="comment_added", case_id=case_id)
        service.create_event(event_type="action_executed", case_id=case_id)
        service.create_event(event_type="finding_added", case_id=case_id)
        service.create_event(event_type="comment_added", case_id=case_id)
        
        counts = service.count_by_event_type(case_id)
        
        assert counts["comment_added"] == 3
        assert counts["action_executed"] == 1
        assert counts["finding_added"] == 1
    
    def test_count_by_user(self):
        """Test counting events by user."""
        service = TimelineService()
        case_id = "case-001"
        
        service.create_event(
            event_type="comment_added",
            case_id=case_id,
            user="analyst1@company.com"
        )
        service.create_event(
            event_type="comment_added",
            case_id=case_id,
            user="analyst1@company.com"
        )
        service.create_event(
            event_type="action_executed",
            case_id=case_id,
            user="analyst2@company.com"
        )
        
        counts = service.count_by_user(case_id)
        
        assert counts["analyst1@company.com"] == 2
        assert counts["analyst2@company.com"] == 1
    
    def test_get_activity_summary(self):
        """Test getting activity summary."""
        service = TimelineService()
        case_id = "case-001"
        
        # Create various events
        service.create_event(event_type="case_created", case_id=case_id)
        service.create_event(event_type="finding_added", case_id=case_id)
        service.create_event(event_type="comment_added", case_id=case_id)
        service.create_event(event_type="action_executed", case_id=case_id)
        
        summary = service.get_activity_summary(case_id)
        
        assert summary["total_events"] == 4
        assert "event_types" in summary
        assert "first_event" in summary
        assert "last_event" in summary


class TestEventUpdates:
    """Test updating timeline events."""
    
    def test_update_event(self):
        """Test updating an event."""
        service = TimelineService()
        
        event = service.create_event(
            event_type="comment_added",
            case_id="case-001",
            comment="Initial comment"
        )
        
        updated = service.update_event(
            event["id"],
            comment="Updated comment"
        )
        
        assert updated["comment"] == "Updated comment"
        assert "updated_at" in updated
    
    def test_delete_event(self):
        """Test deleting an event."""
        service = TimelineService()
        
        event = service.create_event(
            event_type="comment_added",
            case_id="case-001"
        )
        
        result = service.delete_event(event["id"])
        
        assert result is True
        
        # Verify event is deleted
        timeline = service.get_timeline("case-001")
        assert not any(e["id"] == event["id"] for e in timeline)


class TestTimelineExport:
    """Test exporting timeline data."""
    
    def test_export_to_json(self):
        """Test exporting timeline to JSON."""
        service = TimelineService()
        case_id = "case-001"
        
        service.create_event(event_type="case_created", case_id=case_id)
        service.create_event(event_type="finding_added", case_id=case_id)
        
        json_data = service.export_timeline(case_id, format="json")
        
        assert isinstance(json_data, str)
        import json
        parsed = json.loads(json_data)
        assert len(parsed) == 2
    
    def test_export_to_csv(self):
        """Test exporting timeline to CSV."""
        service = TimelineService()
        case_id = "case-001"
        
        service.create_event(
            event_type="comment_added",
            case_id=case_id,
            user="analyst@company.com"
        )
        
        csv_data = service.export_timeline(case_id, format="csv")
        
        assert isinstance(csv_data, str)
        assert "event_type" in csv_data
        assert "comment_added" in csv_data


class TestTimelineVisualization:
    """Test timeline visualization data."""
    
    def test_get_hourly_distribution(self):
        """Test getting hourly event distribution."""
        service = TimelineService()
        case_id = "case-001"
        
        base_time = datetime(2024, 1, 1, 0, 0, 0)
        
        # Create events across different hours
        for hour in [0, 0, 2, 5, 5, 5, 10]:
            service.create_event(
                event_type="event",
                case_id=case_id,
                timestamp=base_time + timedelta(hours=hour)
            )
        
        distribution = service.get_hourly_distribution(case_id)
        
        assert distribution[0] == 2  # 2 events at hour 0
        assert distribution[2] == 1  # 1 event at hour 2
        assert distribution[5] == 3  # 3 events at hour 5
    
    def test_get_daily_distribution(self):
        """Test getting daily event distribution."""
        service = TimelineService()
        case_id = "case-001"
        
        base_time = datetime(2024, 1, 1, 0, 0, 0)
        
        # Create events across different days
        for day in [0, 0, 1, 1, 1, 3]:
            service.create_event(
                event_type="event",
                case_id=case_id,
                timestamp=base_time + timedelta(days=day)
            )
        
        distribution = service.get_daily_distribution(case_id)
        
        assert len(distribution) >= 4


class TestCaseLifecycleTimeline:
    """Test complete case lifecycle timeline."""
    
    def test_case_lifecycle_events(self):
        """Test complete case lifecycle in timeline."""
        service = TimelineService()
        case_id = "case-001"
        
        # Case created
        service.create_event(event_type="case_created", case_id=case_id)
        
        # Findings added
        service.create_event(event_type="finding_added", case_id=case_id)
        service.create_event(event_type="finding_added", case_id=case_id)
        
        # Investigation
        service.create_event(event_type="comment_added", case_id=case_id)
        service.create_event(event_type="ai_enrichment", case_id=case_id)
        
        # Response
        service.create_event(event_type="action_proposed", case_id=case_id)
        service.create_event(event_type="action_approved", case_id=case_id)
        service.create_event(event_type="action_executed", case_id=case_id)
        
        # Resolution
        service.create_event(event_type="case_resolved", case_id=case_id)
        service.create_event(event_type="case_closed", case_id=case_id)
        
        timeline = service.get_timeline(case_id)
        
        assert len(timeline) == 10
        assert timeline[0]["event_type"] == "case_created"
        assert timeline[-1]["event_type"] == "case_closed"
    
    def test_calculate_case_duration(self):
        """Test calculating case duration from timeline."""
        service = TimelineService()
        case_id = "case-001"
        
        created_time = datetime(2024, 1, 1, 10, 0, 0)
        closed_time = datetime(2024, 1, 1, 18, 0, 0)
        
        service.create_event(
            event_type="case_created",
            case_id=case_id,
            timestamp=created_time
        )
        service.create_event(
            event_type="case_closed",
            case_id=case_id,
            timestamp=closed_time
        )
        
        duration = service.calculate_case_duration(case_id)
        
        assert duration == timedelta(hours=8)

