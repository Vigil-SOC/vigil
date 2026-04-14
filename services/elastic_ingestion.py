"""
Elastic Security Ingestion Service - Ingest detection alerts from Elastic Security.

Fetches detection alerts via the Kibana Detections API and converts them to findings.
"""

import logging
import uuid
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from services.siem_ingestion_service import SIEMIngestionService
from services.elastic_service import ElasticService
from core.config import get_integration_config

logger = logging.getLogger(__name__)


class ElasticIngestion(SIEMIngestionService):
    """Elastic Security ingestion service."""

    def __init__(self):
        super().__init__()
        self.siem_name = "Elastic Security"
        self.config = get_integration_config("elastic-siem")
        self._elastic_service: Optional[ElasticService] = None

    def _get_elastic_service(self) -> Optional[ElasticService]:
        if self._elastic_service:
            return self._elastic_service

        try:
            host = self.config.get("elasticsearch_url")
            if not host:
                logger.error("Elastic configuration incomplete: missing elasticsearch_url")
                return None

            self._elastic_service = ElasticService(
                elasticsearch_url=host,
                kibana_url=self.config.get("kibana_url"),
                api_key=self.config.get("api_key"),
                username=self.config.get("username"),
                password=self.config.get("password"),
                verify_ssl=self.config.get("verify_ssl", True),
                index_pattern=self.config.get(
                    "index_pattern", ".alerts-security.alerts-default"
                ),
            )
            return self._elastic_service
        except Exception as e:
            logger.error(f"Error creating Elastic service: {e}")
            return None

    async def fetch_alerts(
        self,
        start_time: Optional[datetime] = None,
        end_time: Optional[datetime] = None,
        limit: int = 100,
    ) -> List[Dict[str, Any]]:
        try:
            svc = self._get_elastic_service()
            if not svc:
                return []

            if not start_time:
                start_time = datetime.utcnow() - timedelta(hours=24)

            time_filter: Dict[str, Any] = {
                "bool": {
                    "filter": [
                        {
                            "range": {
                                "@timestamp": {
                                    "gte": start_time.isoformat() + "Z",
                                    **(
                                        {"lte": end_time.isoformat() + "Z"}
                                        if end_time
                                        else {}
                                    ),
                                }
                            }
                        }
                    ]
                }
            }

            result = await svc.fetch_detection_alerts(
                query=time_filter, size=limit
            )
            if not result:
                return []

            hits = result.get("hits", {}).get("hits", [])
            logger.info(f"Fetched {len(hits)} detection alerts from Elastic Security")
            return hits
        except Exception as e:
            logger.error(f"Error fetching Elastic alerts: {e}")
            return []

    def transform_alert_to_finding(
        self, alert: Dict[str, Any]
    ) -> Optional[Dict[str, Any]]:
        try:
            source = alert.get("_source", {})
            alert_id = alert.get("_id", uuid.uuid4().hex[:12])
            finding_id = f"elastic-{alert_id}"

            # Title from kibana.alert.rule.name or signal.rule.name
            kibana_alert = source.get("kibana.alert.rule.name") or ""
            signal = source.get("signal", {})
            rule = signal.get("rule", {})
            title = (
                kibana_alert
                or rule.get("name")
                or source.get("rule", {}).get("name")
                or "Elastic Security Alert"
            )

            description = (
                source.get("kibana.alert.rule.description")
                or rule.get("description")
                or source.get("message", "")
            )

            # Severity
            raw_severity = (
                source.get("kibana.alert.severity")
                or rule.get("severity")
                or source.get("event", {}).get("severity")
                or "medium"
            )
            severity = self.normalize_severity(raw_severity)

            # Entities
            entity_context: Dict[str, List[str]] = {
                "src_ips": [],
                "dest_ips": [],
                "hostnames": [],
                "usernames": [],
            }

            # Source / destination IPs
            src = source.get("source", {})
            dst = source.get("destination", {})
            if src.get("ip"):
                entity_context["src_ips"].append(str(src["ip"]))
            if dst.get("ip"):
                entity_context["dest_ips"].append(str(dst["ip"]))

            # Host
            host = source.get("host", {})
            if host.get("name"):
                entity_context["hostnames"].append(str(host["name"]))

            # User
            user = source.get("user", {})
            if user.get("name"):
                entity_context["usernames"].append(str(user["name"]))

            # MITRE ATT&CK from rule threat metadata
            mitre_predictions: Dict[str, float] = {}
            threats = (
                source.get("kibana.alert.rule.threat", [])
                or rule.get("threat", [])
            )
            if isinstance(threats, list):
                for threat in threats:
                    technique = threat.get("technique", [])
                    if isinstance(technique, list):
                        for t in technique:
                            tid = t.get("id")
                            if tid:
                                mitre_predictions[tid] = 0.9

            return {
                "finding_id": finding_id,
                "data_source": "elastic",
                "timestamp": source.get("@timestamp", datetime.utcnow().isoformat()),
                "severity": severity,
                "status": "new",
                "title": title,
                "description": description[:500] if description else "",
                "entity_context": entity_context,
                "raw_event": alert,
                "anomaly_score": 0.5,
                "mitre_predictions": mitre_predictions,
                "embedding": [],
                "metadata": {
                    "elastic_alert_id": alert_id,
                    "rule_id": (
                        source.get("kibana.alert.rule.uuid")
                        or rule.get("id", "")
                    ),
                    "rule_name": title,
                    "index": alert.get("_index", ""),
                    "kibana_case_ids": source.get("kibana.alert.case_ids", []),
                },
            }
        except Exception as e:
            logger.error(f"Error transforming Elastic alert: {e}")
            return None

    async def update_upstream_alert_status(
        self,
        alert_id: str,
        status: str,
        note: Optional[str] = None,
    ) -> bool:
        """Push a status change back to Elastic Security."""
        svc = self._get_elastic_service()
        if not svc:
            return False

        elastic_status_map = {
            "acknowledged": "acknowledged",
            "in_progress": "acknowledged",
            "closed": "closed",
            "resolved": "closed",
            "open": "open",
            "new": "open",
        }
        es_status = elastic_status_map.get(status, status)
        return await svc.update_alert_status([alert_id], es_status)
