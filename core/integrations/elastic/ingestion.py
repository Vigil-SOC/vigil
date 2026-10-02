"""
Elastic Security Ingestion Service - Ingest detection alerts from Elastic Security.

Fetches detection alerts via the Kibana Detections API and converts them to
findings. Without a Kibana URL the configured index pattern is read directly:
that is how alerts come off an OpenSearch-based Wazuh indexer, which has no
Kibana and stores every alert as a document in ``wazuh-alerts-4.x-*``.
"""

import logging
import uuid
from datetime import datetime, timedelta
from typing import Any, Dict, List, Mapping, Optional

from core.ingestion.siem_ingestion_service import SIEMIngestionService
from core.integrations._base.config import resolve
from core.integrations._base.ids import EXTERNAL_ID_MAX, FINDING_ID_MAX, fit_id
from core.integrations.elastic.client import ElasticService
from core.integrations.elastic.descriptor import ELASTIC
from core.time import utcnow

logger = logging.getLogger(__name__)

# Wazuh rule levels run 0-16; 7 is where Wazuh's own classification starts
# calling an alert significant.
DEFAULT_MIN_RULE_LEVEL = 7
MAX_RULE_LEVEL = 16


def min_rule_level_from_config(config: Mapping[str, Any]) -> int:
    """The minimum Wazuh rule level to ingest, defaulting to 7.

    The Settings form saves a cleared number field as 0, and level-0 rules
    never raise alerts, so 0 means unset rather than "ingest everything".
    """
    level = config.get("min_rule_level")
    if not level:
        return DEFAULT_MIN_RULE_LEVEL
    try:
        return max(1, min(MAX_RULE_LEVEL, int(level)))
    except (TypeError, ValueError):
        return DEFAULT_MIN_RULE_LEVEL


def _rule_level(level: Any) -> Optional[int]:
    try:
        return int(level)
    except (TypeError, ValueError):
        return None


def severity_for_rule_level(level: Any) -> str:
    """Map a Wazuh rule level (0-16) onto Vigil's severity scale."""
    value = _rule_level(level)
    if value is None:
        return "medium"
    if value >= 12:
        return "critical"
    if value >= 10:
        return "high"
    if value >= 7:
        return "medium"
    if value >= 4:
        return "low"
    return "info"


def is_wazuh_alert(source: Mapping[str, Any]) -> bool:
    """A Wazuh indexer document: its ``rule`` carries a level.

    Elastic Security alerts keep their rule under ``kibana.alert.rule.*`` (or
    legacy ``signal.rule``) and the ECS ``rule`` field set has no ``level``;
    a detection alert wrapping a forwarded Wazuh event stays a detection alert.
    """
    if "signal" in source or "kibana" in source:
        return False
    if any(k.startswith("kibana.alert.") for k in source):
        return False
    rule = source.get("rule")
    return isinstance(rule, dict) and rule.get("level") is not None


def _as_list(value: Any) -> List[Any]:
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def _add(bucket: List[str], value: Any) -> None:
    if value and str(value) not in bucket:
        bucket.append(str(value))


class ElasticIngestion(SIEMIngestionService):
    """Elastic Security ingestion service."""

    def __init__(self):
        super().__init__()
        self.siem_name = "Elastic Security"
        self.config = resolve(ELASTIC)
        self.min_rule_level = min_rule_level_from_config(self.config)
        self._elastic_service: Optional[ElasticService] = None

    def _get_elastic_service(self) -> Optional[ElasticService]:
        if self._elastic_service:
            return self._elastic_service

        try:
            host = self.config.get("elasticsearch_url")
            if not host:
                logger.error(
                    "Elastic configuration incomplete: missing elasticsearch_url"
                )
                return None

            # resolve() always returns every declared field, so a .get(k, True)
            # default would never fire — verify_ssl is present-but-None when unset.
            verify = (
                True
                if self.config.get("verify_ssl") is None
                else self.config.get("verify_ssl")
            )
            self._elastic_service = ElasticService(
                elasticsearch_url=host,
                kibana_url=self.config.get("kibana_url"),
                api_key=self.config.get("api_key"),
                username=self.config.get("username"),
                password=self.config.get("password"),
                verify_ssl=verify,
                index_pattern=self.config.get("index_pattern")
                or ".alerts-security.alerts-default",
                ca_cert_path=self.config.get("ca_cert_path"),
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
        oldest_first: bool = False,
    ) -> List[Dict[str, Any]]:
        """Fetch detection alerts in the window.

        ``oldest_first`` is what federation asks for: a batch that fills
        ``limit`` must be a contiguous oldest-first prefix of the window so the
        cursor can stop at its newest alert. The default keeps the newest-first
        order the daemon poller has always read.
        """
        try:
            svc = self._get_elastic_service()
            if not svc:
                return []

            if not start_time:
                start_time = utcnow() - timedelta(hours=24)

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

            # No Kibana: read the index pattern directly (a Wazuh indexer).
            # Always oldest first, with _doc as a stable tiebreaker (sorting on
            # _id needs id fielddata on Elasticsearch 8).
            if not svc.kibana_url:
                time_filter["bool"]["filter"].append(
                    {"range": {"rule.level": {"gte": self.min_rule_level}}}
                )
                result = await svc.search(
                    query=time_filter,
                    size=limit,
                    sort=[{"@timestamp": {"order": "asc"}}, {"_doc": {"order": "asc"}}],
                )
                if result is None:
                    raise RuntimeError(
                        f"Elastic index search failed: {svc.index_pattern}"
                    )
                hits = result.get("hits", {}).get("hits", [])
                logger.info(f"Fetched {len(hits)} alerts from {svc.index_pattern}")
                return hits

            result = await svc.fetch_detection_alerts(
                query=time_filter,
                size=limit,
                sort_order="asc" if oldest_first else "desc",
            )
            if result is None:
                # The client returns None on any request failure.
                raise RuntimeError("Elastic detection alert search failed")

            hits = result.get("hits", {}).get("hits", [])
            logger.info(f"Fetched {len(hits)} detection alerts from Elastic Security")
            return hits
        except Exception as e:
            logger.error(f"Error fetching Elastic alerts: {e}")
            # Raise, not []: federation must record the failure and keep its cursor.
            raise

    def transform_alert_to_finding(
        self, alert: Dict[str, Any]
    ) -> Optional[Dict[str, Any]]:
        try:
            source = alert.get("_source", {})
            alert_id = str(alert.get("_id", uuid.uuid4().hex[:12]))
            # Kibana detection alert ids are 64-char SHA-256 hex, which don't
            # fit findings.finding_id. Ids that fit are left as they were; the
            # full id stays in external_id (bounded) and in
            # metadata.elastic_alert_id, which upstream status sync uses.
            finding_id = fit_id("elastic-", alert_id, FINDING_ID_MAX)

            if is_wazuh_alert(source):
                return self._wazuh_finding(alert, source, alert_id, finding_id)

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
            threats = source.get("kibana.alert.rule.threat", []) or rule.get(
                "threat", []
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
                "external_id": fit_id("", alert_id, EXTERNAL_ID_MAX),
                "data_source": "elastic",
                "timestamp": source.get("@timestamp", utcnow().isoformat()),
                "severity": severity,
                "status": "new",
                "title": title,
                "description": description[:500] if description else "",
                "entity_context": entity_context,
                "raw_event": alert,
                "anomaly_score": 0.5,
                "mitre_predictions": mitre_predictions,
                "metadata": {
                    "elastic_alert_id": alert_id,
                    "rule_id": (
                        source.get("kibana.alert.rule.uuid") or rule.get("id", "")
                    ),
                    "rule_name": title,
                    "index": alert.get("_index", ""),
                    "kibana_case_ids": source.get("kibana.alert.case_ids", []),
                },
            }
        except Exception as e:
            logger.error(f"Error transforming Elastic alert: {e}")
            return None

    def _wazuh_finding(
        self,
        alert: Dict[str, Any],
        source: Dict[str, Any],
        alert_id: str,
        finding_id: str,
    ) -> Dict[str, Any]:
        """A finding from a Wazuh alert document (Wazuh's schema, not ECS)."""
        rule = source.get("rule") or {}
        agent = source.get("agent") or {}
        data = source.get("data") or {}
        win = data.get("win") or {}
        eventdata = win.get("eventdata") or {}
        level = rule.get("level")

        title = rule.get("description") or "Wazuh Alert"
        # Windows eventchannel alerts carry no full_log.
        description = (
            source.get("full_log") or (win.get("system") or {}).get("message") or title
        )

        entity_context: Dict[str, List[str]] = {
            "src_ips": [],
            "dest_ips": [],
            "hostnames": [],
            "usernames": [],
        }
        _add(entity_context["src_ips"], data.get("srcip"))
        _add(entity_context["src_ips"], eventdata.get("ipAddress"))
        _add(entity_context["dest_ips"], data.get("dstip"))
        _add(entity_context["hostnames"], agent.get("name"))
        if agent.get("id") == "000":
            # Agent 000 is the manager; for syslog it relays, the sending host
            # is the one the predecoder saw.
            _add(
                entity_context["hostnames"],
                (source.get("predecoder") or {}).get("hostname"),
            )
        for user in (
            data.get("srcuser"),
            data.get("dstuser"),
            eventdata.get("targetUserName"),
            eventdata.get("subjectUserName"),
        ):
            _add(entity_context["usernames"], user)

        mitre = rule.get("mitre") or {}
        parsed_level = _rule_level(level)
        return {
            "finding_id": finding_id,
            "external_id": fit_id("", alert_id, EXTERNAL_ID_MAX),
            "data_source": "elastic",
            "timestamp": source.get("@timestamp")
            or source.get("timestamp")
            or utcnow().isoformat(),
            "severity": severity_for_rule_level(level),
            "status": "new",
            "title": title,
            "description": str(description)[:500],
            "entity_context": entity_context,
            "raw_event": alert,
            "anomaly_score": (
                round(min(max(parsed_level, 0), 15) / 15, 2)
                if parsed_level is not None
                else 0.5
            ),
            "mitre_predictions": {
                str(tid): 0.9 for tid in _as_list(mitre.get("id")) if tid
            },
            "metadata": {
                "elastic_alert_id": alert_id,
                "vendor": "wazuh",
                "wazuh_alert_id": source.get("id", alert_id),
                "rule_id": rule.get("id", ""),
                "rule_level": level,
                "rule_name": title,
                "rule_groups": _as_list(rule.get("groups")),
                "mitre_tactics": _as_list(mitre.get("tactic")),
                "mitre_techniques": _as_list(mitre.get("technique")),
                "agent_id": agent.get("id", ""),
                "agent_name": agent.get("name", ""),
                "index": alert.get("_index", ""),
            },
        }

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
        if not svc.kibana_url:
            # Alert status lives in Kibana; an indexer has none to update.
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
