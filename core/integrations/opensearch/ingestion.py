"""
OpenSearch Ingestion Service - Ingest Security Analytics findings.

Detections in OpenSearch come from the Security Analytics plugin: a
detector's Sigma rules match ingested log documents and produce *findings*,
stored as documents in ``.opensearch-sap-<log-type>-findings-*`` indices.
A finding is deliberately thin — its id, the monitor that produced it
(``monitor_id`` / ``monitor_name``, the latter being the detector's name), the
rules that matched (``queries``, each with the rule's id, name and tags), the
source index, the ids of the triggering documents (``related_doc_ids``), and a
``timestamp`` in epoch milliseconds — so ingestion reads the findings indices directly with the search API, the same
way the Elastic slice reads a Wazuh indexer that has no Kibana.
"""

import logging
import re
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Mapping, Optional

from core.ingestion.siem_ingestion_service import SIEMIngestionService
from core.integrations._base.config import resolve
from core.integrations._base.ids import EXTERNAL_ID_MAX, FINDING_ID_MAX, fit_id
from core.integrations.opensearch.client import OpenSearchService
from core.integrations.opensearch.descriptor import OPENSEARCH
from core.time import utcnow

logger = logging.getLogger(__name__)

# Sigma attack tags look like "attack.t1059.001" or "attack.execution".
_TECHNIQUE_TAG = re.compile(r"^attack\.(t\d{4}(?:\.\d{3})?)$", re.IGNORECASE)

# Security Analytics stamps each matched rule's Sigma level into its tags as a
# bare word ("critical"), next to the log type and the "attack.*" tags.
_SIGMA_LEVELS = frozenset({"informational", "low", "medium", "high", "critical"})

# ".opensearch-sap-<log-type>-findings-<suffix>"
_FINDINGS_INDEX = re.compile(r"^\.opensearch-sap-(.+)-findings-")


def _as_list(value: Any) -> List[Any]:
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def _add(bucket: List[str], value: Any) -> None:
    if value and str(value) not in bucket:
        bucket.append(str(value))


def _queries(source: Mapping[str, Any]) -> List[Mapping[str, Any]]:
    """The matched-rule entries of a finding, as mappings.

    Security Analytics stores ``queries`` as a list of objects carrying at
    least the rule ``id``; a bare string entry is a rule id on its own.
    """
    out: List[Mapping[str, Any]] = []
    for entry in _as_list(source.get("queries")):
        if isinstance(entry, Mapping):
            out.append(entry)
        elif entry:
            out.append({"id": entry})
    return out


def rule_ids_of(source: Mapping[str, Any]) -> List[str]:
    """Ids of the Sigma rules a finding matched."""
    ids: List[str] = []
    for query in _queries(source):
        _add(ids, query.get("id"))
    return ids


def severity_of(source: Mapping[str, Any]) -> Optional[str]:
    """The Sigma level a matched rule carries in its tags, if any."""
    for query in _queries(source):
        for tag in _as_list(query.get("tags")):
            level = str(tag).strip().lower()
            if level in _SIGMA_LEVELS:
                return level
    return None


def mitre_from_tags(source: Mapping[str, Any]) -> Dict[str, float]:
    """MITRE ATT&CK technique ids from Sigma ``attack.*`` tags on the rules."""
    predictions: Dict[str, float] = {}
    for query in _queries(source):
        for tag in _as_list(query.get("tags")):
            match = _TECHNIQUE_TAG.match(str(tag).strip())
            if match:
                predictions[match.group(1).upper()] = 0.9
    return predictions


def finding_time(source: Mapping[str, Any]) -> Optional[datetime]:
    """A finding's creation time as naive UTC.

    The findings index maps ``timestamp`` as a ``long`` of epoch milliseconds.
    """
    raw = source.get("timestamp")
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return None
    try:
        return datetime.fromtimestamp(raw / 1000, tz=timezone.utc).replace(tzinfo=None)
    except (OverflowError, OSError, ValueError):
        return None


def _epoch_ms(moment: datetime) -> int:
    return int(moment.replace(tzinfo=timezone.utc).timestamp() * 1000)


def log_type_of(index_name: str) -> str:
    """The detector log type encoded in a findings index name, if any."""
    match = _FINDINGS_INDEX.match(index_name or "")
    return match.group(1) if match else ""


class OpenSearchIngestion(SIEMIngestionService):
    """OpenSearch Security Analytics ingestion service."""

    def __init__(self):
        super().__init__()
        self.siem_name = "OpenSearch"
        self.config = resolve(OPENSEARCH)
        self._opensearch_service: Optional[OpenSearchService] = None

    def _get_opensearch_service(self) -> Optional[OpenSearchService]:
        if self._opensearch_service:
            return self._opensearch_service

        # Constructor errors propagate: a configured source that cannot build
        # its client is failing, not empty (#1573). A missing URL stays a quiet [].
        host = self.config.get("opensearch_url")
        if not host:
            logger.error("OpenSearch configuration incomplete: missing opensearch_url")
            return None

        # resolve() always returns every declared field, so a .get(k, True)
        # default would never fire — verify_ssl is present-but-None when unset.
        verify = (
            True
            if self.config.get("verify_ssl") is None
            else self.config.get("verify_ssl")
        )
        self._opensearch_service = OpenSearchService(
            opensearch_url=host,
            dashboards_url=self.config.get("dashboards_url"),
            username=self.config.get("username"),
            password=self.config.get("password"),
            verify_ssl=verify,
            index_pattern=self.config.get("index_pattern")
            or ".opensearch-sap-*-findings-*",
            ca_cert_path=self.config.get("ca_cert_path"),
        )
        return self._opensearch_service

    async def fetch_alerts(
        self,
        start_time: Optional[datetime] = None,
        end_time: Optional[datetime] = None,
        limit: int = 100,
        oldest_first: bool = False,
    ) -> List[Dict[str, Any]]:
        """Fetch Security Analytics findings in the window.

        Findings carry their creation time in ``timestamp`` (epoch
        milliseconds, not the ECS ``@timestamp``). ``oldest_first`` is what federation asks for: a batch
        that fills ``limit`` must be a contiguous oldest-first prefix of the
        window so the cursor can stop at its newest finding. ``_doc`` is the
        stable tiebreaker, as in the Elastic indexer path.
        """
        try:
            svc = self._get_opensearch_service()
            if not svc:
                return []

            if not start_time:
                start_time = utcnow() - timedelta(hours=24)

            window: Dict[str, int] = {"gte": _epoch_ms(start_time)}
            if end_time:
                window["lte"] = _epoch_ms(end_time)
            query: Dict[str, Any] = {
                "bool": {"filter": [{"range": {"timestamp": window}}]}
            }
            order = "asc" if oldest_first else "desc"
            result = await svc.search(
                query=query,
                size=limit,
                sort=[{"timestamp": {"order": order}}, {"_doc": {"order": order}}],
            )
            if result is None:
                # The client returns None on any request failure.
                raise RuntimeError(
                    f"OpenSearch findings search failed: {svc.index_pattern}"
                )

            hits = result.get("hits", {}).get("hits", [])
            logger.info(f"Fetched {len(hits)} findings from {svc.index_pattern}")
            return hits
        except Exception as e:
            logger.error(f"Error fetching OpenSearch findings: {e}")
            # Raise, not []: federation must record the failure and keep its cursor.
            raise

    def transform_alert_to_finding(
        self, alert: Dict[str, Any]
    ) -> Optional[Dict[str, Any]]:
        try:
            source = alert.get("_source", {})
            # The finding's own id lives in the document; fall back to the
            # hit id, which is the same value in the findings indices.
            finding_ref = str(
                source.get("id") or alert.get("_id") or uuid.uuid4().hex[:12]
            )
            finding_id = fit_id("opensearch-", finding_ref, FINDING_ID_MAX)

            rule_ids = rule_ids_of(source)
            monitor_id = str(source.get("monitor_id") or "")
            detector_name = str(source.get("monitor_name") or "")
            source_index = str(source.get("index") or "")
            related_doc_ids = [str(d) for d in _as_list(source.get("related_doc_ids"))]

            title = ""
            for query in _queries(source):
                title = str(query.get("name") or query.get("title") or "")
                if title:
                    break
            if not title:
                title = "OpenSearch Security Analytics Finding"

            description_parts = [title]
            if detector_name:
                description_parts.append(f"Detector: {detector_name}")
            if rule_ids:
                description_parts.append(f"Matched rules: {', '.join(rule_ids)}")
            if source_index:
                description_parts.append(f"Source index: {source_index}")
            if related_doc_ids:
                description_parts.append(
                    f"Triggering documents: {len(related_doc_ids)}"
                )
            description = ". ".join(description_parts)

            raw_severity = severity_of(source)
            severity = (
                self.normalize_severity(raw_severity) if raw_severity else "medium"
            )
            created = finding_time(source)

            # Raw findings carry no event payload, so entities are normally
            # empty; an enriched document that embeds ECS fields still yields
            # them, as in the Elastic transform.
            entity_context: Dict[str, List[str]] = {
                "src_ips": [],
                "dest_ips": [],
                "hostnames": [],
                "usernames": [],
            }
            src = source.get("source", {})
            dst = source.get("destination", {})
            if isinstance(src, Mapping) and src.get("ip"):
                entity_context["src_ips"].append(str(src["ip"]))
            if isinstance(dst, Mapping) and dst.get("ip"):
                entity_context["dest_ips"].append(str(dst["ip"]))
            host = source.get("host", {})
            if isinstance(host, Mapping) and host.get("name"):
                entity_context["hostnames"].append(str(host["name"]))
            user = source.get("user", {})
            if isinstance(user, Mapping) and user.get("name"):
                entity_context["usernames"].append(str(user["name"]))

            return {
                "finding_id": finding_id,
                "external_id": fit_id("", finding_ref, EXTERNAL_ID_MAX),
                "data_source": "opensearch",
                "timestamp": (
                    created.isoformat() + "Z" if created else utcnow().isoformat()
                ),
                "severity": severity,
                "status": "new",
                "title": title,
                "description": description[:500],
                "entity_context": entity_context,
                "raw_event": alert,
                "anomaly_score": 0.5,
                "mitre_predictions": mitre_from_tags(source),
                "metadata": {
                    "opensearch_finding_id": finding_ref,
                    "monitor_id": monitor_id,
                    "detector_name": detector_name,
                    "triggered_rule_ids": rule_ids,
                    "related_doc_ids": related_doc_ids,
                    "source_index": source_index,
                    "log_type": log_type_of(alert.get("_index", "")),
                    "index": alert.get("_index", ""),
                },
            }
        except Exception as e:
            logger.error(f"Error transforming OpenSearch finding: {e}")
            return None

    async def update_upstream_alert_status(
        self,
        alert_id: str,
        status: str,
        note: Optional[str] = None,
    ) -> bool:
        """Push a status change back to OpenSearch.

        Security Analytics findings are immutable records of a rule match —
        the plugin exposes no status to update (acknowledgement lives on
        Alerting-plugin alerts, a different document type), so there is
        nothing to sync back.
        """
        return False
