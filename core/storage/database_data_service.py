import json
import logging
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from core.config import is_demo_mode
from core.exceptions import DatabaseError
from core.storage.connection import (
    SchemaDriftError,
    get_db_manager,
    init_database,
)
from core.storage.schemas import CaseSchema, FindingSchema
from core.storage.service import DatabaseService

logger = logging.getLogger(__name__)


def _optional_score(value: Any) -> Optional[float]:
    """Parse a score; missing, empty, and NaN stay None (do not coerce to 0.0)."""
    if value is None or value == "":
        return None
    parsed = float(value)
    if parsed != parsed:
        return None
    return parsed


class DatabaseDataService:
    # Minimum seconds between reconnection attempts when DB is unreachable.
    _RECONNECT_INTERVAL_SECONDS = 10.0

    def __init__(self, demo_data=None):
        self._db_service = None
        self._db_connected = False
        self._last_reconnect_attempt = 0.0
        self._demo_mode = is_demo_mode()
        self._demo_service = None

        if self._demo_mode:
            logger.info("Demo mode enabled - using generated sample data")
            from core.platform.demo_data_service import DemoDataService

            self._demo_service = demo_data or DemoDataService()
        else:
            self._init_database()

    def _init_database(self):
        try:
            init_database(echo=False, create_tables=True)
            db_manager = get_db_manager()
            if not db_manager.health_check():
                raise DatabaseError("Database health check failed")
            self._db_service = DatabaseService()
            self._db_connected = True
            logger.info("PostgreSQL connection established")
        except SchemaDriftError:
            # DB_STRICT_SCHEMA is set, so the operator asked for this to be
            # fatal (#562).
            self._db_connected = False
            self._db_service = None
            raise
        except Exception as e:
            self._db_connected = False
            self._db_service = None
            logger.warning(f"PostgreSQL not available: {e}")

    @property
    def _db_available(self) -> bool:
        """True when the Postgres connection is healthy.

        If currently disconnected, transparently retries `_init_database`
        at most once per `_RECONNECT_INTERVAL_SECONDS` so the service
        recovers automatically when Postgres becomes reachable again.
        """
        if self._db_connected:
            return True
        if self._demo_mode:
            return False
        now = time.monotonic()
        if now - self._last_reconnect_attempt < self._RECONNECT_INTERVAL_SECONDS:
            return False
        self._last_reconnect_attempt = now
        self._init_database()
        return self._db_connected

    def is_using_database(self) -> bool:
        return self._db_available and self._db_service is not None

    def is_demo_mode(self) -> bool:
        return self._demo_mode

    def get_backend_info(self) -> dict:
        if self._demo_mode:
            return {"backend": "demo", "database_available": False, "demo_mode": True}
        if self._db_available:
            return {
                "backend": "postgresql",
                "database_available": True,
                "demo_mode": False,
            }
        return {"backend": "none", "database_available": False, "demo_mode": False}

    def get_findings(
        self,
        limit: int = 10000,
        offset: int = 0,
        severity: Optional[str] = None,
        data_source: Optional[str] = None,
        cluster_id: Optional[str] = None,
        min_anomaly_score: Optional[float] = None,
        status: Optional[str] = None,
        search_query: Optional[str] = None,
        sort_by: str = "timestamp",
        sort_order: str = "desc",
    ) -> List[Dict]:
        if self._demo_mode and self._demo_service:
            return self._demo_service.get_findings(limit)
        if self._db_available:
            try:
                findings = self._db_service.get_findings(
                    severity=severity,
                    data_source=data_source,
                    cluster_id=cluster_id,
                    min_anomaly_score=min_anomaly_score,
                    status=status,
                    search_query=search_query,
                    limit=limit,
                    offset=offset,
                    sort_by=sort_by,
                    sort_order=sort_order,
                )
                return FindingSchema.dump_many(findings)
            except Exception as e:
                logger.error(f"Error getting findings from DB: {e}")
                return []
        return []

    def get_findings_missing_enrichment(
        self, limit: int = 100, max_age_hours: Optional[int] = None
    ) -> List[Dict]:
        """Findings stored but never enriched (ai_enrichment IS NULL)."""
        if not self._db_available or not self._db_service:
            return []
        try:
            return self._db_service.get_findings_missing_enrichment(
                limit=limit, max_age_hours=max_age_hours
            )
        except Exception as e:
            logger.error(f"Error in get_findings_missing_enrichment: {e}")
            return []

    def count_findings(
        self,
        severity: Optional[str] = None,
        data_source: Optional[str] = None,
        cluster_id: Optional[str] = None,
        min_anomaly_score: Optional[float] = None,
        status: Optional[str] = None,
        search_query: Optional[str] = None,
    ) -> int:
        if self._demo_mode and self._demo_service:
            return len(self._demo_service.get_findings(10000))
        if self._db_available:
            try:
                return self._db_service.count_findings(
                    severity=severity,
                    data_source=data_source,
                    cluster_id=cluster_id,
                    min_anomaly_score=min_anomaly_score,
                    status=status,
                    search_query=search_query,
                )
            except Exception as e:
                logger.error(f"Error counting findings from DB: {e}")
                return 0
        return 0

    def get_finding(self, finding_id: str) -> Optional[Dict]:
        if self._demo_mode and self._demo_service:
            return self._demo_service.get_finding(finding_id)
        if self._db_available:
            try:
                finding = self._db_service.get_finding(finding_id)
                return FindingSchema.dump(finding) if finding else None
            except Exception as e:
                logger.error(f"Error getting finding from DB: {e}")
                return None
        return None

    def get_findings_by_technique(
        self, technique_id: str, limit: Optional[int] = None
    ) -> List[Dict]:
        """Findings predicting ``technique_id`` from the child table. DB only."""
        if not self._db_available or not self._db_service:
            return []
        try:
            findings = self._db_service.get_findings_by_technique(
                technique_id, limit=limit
            )
            return FindingSchema.dump_many(findings)
        except Exception as e:
            logger.error(f"Error getting findings by technique from DB: {e}")
            return []

    def get_technique_max_confidence(self) -> Dict[str, float]:
        if not self._db_available or not self._db_service:
            return {}
        try:
            return self._db_service.get_technique_max_confidence()
        except Exception as e:
            logger.error(f"Error getting technique max confidence from DB: {e}")
            return {}

    def get_technique_severity_counts(
        self,
        min_confidence: float = 0.0,
        start_time=None,
        end_time=None,
    ) -> list:
        if not self._db_available or not self._db_service:
            return []
        try:
            return self._db_service.get_technique_severity_counts(
                min_confidence=min_confidence,
                start_time=start_time,
                end_time=end_time,
            )
        except Exception as e:
            logger.error(f"Error getting technique severity counts from DB: {e}")
            return []

    def get_technique_occurrence_counts(self) -> Dict[str, int]:
        if not self._db_available or not self._db_service:
            return {}
        try:
            return self._db_service.get_technique_occurrence_counts()
        except Exception as e:
            logger.error(f"Error getting technique occurrence counts from DB: {e}")
            return {}

    def create_finding(self, finding_data: Dict) -> Optional[Dict]:
        if self._demo_mode and self._demo_service:
            return self._demo_service.create_finding(finding_data)
        if self._db_available:
            try:
                finding = self._db_service.create_finding(
                    finding_id=finding_data.get("finding_id"),
                    mitre_predictions=finding_data.get("mitre_predictions", {}),
                    anomaly_score=_optional_score(finding_data.get("anomaly_score")),
                    timestamp=finding_data.get("timestamp") or None,
                    data_source=finding_data.get("data_source", "imported"),
                    description=finding_data.get("description"),
                    entity_context=finding_data.get("entity_context"),
                    evidence_links=finding_data.get("evidence_links"),
                    cluster_id=finding_data.get("cluster_id"),
                    severity=finding_data.get("severity"),
                    status=finding_data.get("status", "new"),
                )
                return FindingSchema.dump(finding) if finding else None
            except Exception as e:
                logger.error(f"Error creating finding in DB: {e}")
                return None
        return None

    def update_finding(self, finding_id: str, **updates) -> bool:
        if self._demo_mode and self._demo_service:
            return self._demo_service.update_finding(finding_id, **updates)
        if self._db_available:
            try:
                return self._db_service.update_finding(finding_id, **updates)
            except Exception as e:
                logger.error(f"Error updating finding in DB: {e}")
                return False
        return False

    def get_cases(self, limit: int = 10000) -> List[Dict]:
        if self._demo_mode and self._demo_service:
            return self._demo_service.get_cases(limit)
        if self._db_available:
            try:
                cases = self._db_service.get_cases(limit=limit)
                return CaseSchema.dump_many(cases)
            except Exception as e:
                logger.error(f"Error getting cases from DB: {e}")
                return []
        return []

    def get_case(self, case_id: str) -> Optional[Dict]:
        if self._demo_mode and self._demo_service:
            return self._demo_service.get_case(case_id)
        if self._db_available:
            try:
                case = self._db_service.get_case(case_id, include_findings=True)
                return CaseSchema.dump(case) if case else None
            except Exception as e:
                logger.error(f"Error getting case from DB: {e}")
                return None
        return None

    def create_case(
        self,
        title: str,
        finding_ids: List[str],
        priority: str = "medium",
        description: str = "",
        status: str = "open",
        case_id: Optional[str] = None,
    ) -> Optional[Dict]:
        if self._demo_mode and self._demo_service:
            return self._demo_service.create_case(
                title, finding_ids, priority, description, status, case_id
            )

        # Minted here unless the caller brought one. A caller that can derive a
        # stable id -- an agent handoff, whose document arrives twice -- gets an
        # idempotent create out of it, because cases.case_id is the primary key and
        # the second insert loses rather than opening a second case.
        if case_id is None:
            case_id = (
                f"case-{datetime.now().strftime('%Y-%m-%d')}-{uuid.uuid4().hex[:8]}"
            )

        if self._db_available:
            try:
                case = self._db_service.create_case(
                    case_id=case_id,
                    title=title,
                    finding_ids=finding_ids,
                    description=description,
                    status=status,
                    priority=priority,
                )
                return CaseSchema.dump(case) if case else None
            except Exception as e:
                logger.error(f"Error creating case in DB: {e}")
                return None
        return None

    def update_case(self, case_id: str, **updates) -> bool:
        if self._demo_mode and self._demo_service:
            return self._demo_service.update_case(case_id, **updates)
        if self._db_available:
            try:
                return self._db_service.update_case(case_id, **updates)
            except Exception as e:
                logger.error(f"Error updating case in DB: {e}")
                return False
        return False

    def delete_case(self, case_id: str) -> bool:
        if self._demo_mode and self._demo_service:
            return self._demo_service.delete_case(case_id)
        if self._db_available:
            try:
                return self._db_service.delete_case(case_id)
            except Exception as e:
                logger.error(f"Error deleting case from DB: {e}")
                return False
        return False

    def get_findings_by_case(self, case_id: str) -> List[Dict]:
        """Get all findings associated with a case.

        Args:
            case_id: ID of the case

        Returns:
            List of finding dictionaries
        """
        if self._demo_mode and self._demo_service:
            # Get case and extract finding IDs
            case = self._demo_service.get_case(case_id)
            if case and "finding_ids" in case:
                findings = []
                for fid in case["finding_ids"]:
                    finding = self._demo_service.get_finding(fid)
                    if finding:
                        findings.append(finding)
                return findings
            return []

        if self._db_available:
            try:
                # Get case with findings
                case = self._db_service.get_case(case_id, include_findings=True)
                if case and case.findings:
                    return FindingSchema.dump_many(case.findings)
                return []
            except Exception as e:
                logger.error(f"Error getting findings for case from DB: {e}")
                return []
        return []

    def add_finding_to_case(self, case_id: str, finding_id: str) -> bool:
        """Add a finding to an existing case.

        Args:
            case_id: The case ID
            finding_id: The finding ID to add

        Returns:
            True if successful, False otherwise
        """
        if self._demo_mode and self._demo_service:
            return self._demo_service.add_finding_to_case(case_id, finding_id)

        if self._db_available:
            try:
                return self._db_service.add_finding_to_case(case_id, finding_id)
            except Exception as e:
                logger.error(f"Error adding finding to case in DB: {e}")
                return False
        return False

    def export_findings(self, output_path: Path, fmt: str = "json") -> bool:
        findings = self.get_findings()
        try:
            with open(output_path, "w") as f:
                if fmt == "jsonl":
                    for finding in findings:
                        f.write(json.dumps(finding) + "\n")
                else:
                    json.dump({"findings": findings}, f, indent=2)
            return True
        except Exception as e:
            logger.error(f"Error exporting findings: {e}")
            return False
