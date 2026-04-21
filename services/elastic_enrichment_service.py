"""Elastic enrichment service with Claude AI analysis."""

import json
import logging
import re
from datetime import datetime
from typing import Any, Dict, List, Optional

from services.elastic_service import ElasticService
from services.database_data_service import DatabaseDataService

logger = logging.getLogger(__name__)


class ElasticEnrichmentService:
    """Service for enriching cases and findings with Elasticsearch data."""

    def __init__(
        self,
        elastic_service: ElasticService,
        claude_service=None,
    ):
        self.elastic_service = elastic_service
        self.claude_service = claude_service
        self.data_service = DatabaseDataService()

    # ------------------------------------------------------------------
    # IOC extraction
    # ------------------------------------------------------------------

    def extract_indicators(
        self, case: Dict, findings: List[Dict]
    ) -> Dict[str, List[str]]:
        """Extract IOCs from case and findings."""
        ip_pattern = r"\b(?:[0-9]{1,3}\.){3}[0-9]{1,3}\b"
        hash_pattern = (
            r"\b[a-fA-F0-9]{32}\b|\b[a-fA-F0-9]{40}\b|\b[a-fA-F0-9]{64}\b"
        )

        indicators: Dict[str, set] = {
            "ips": set(),
            "hashes": set(),
            "usernames": set(),
            "hostnames": set(),
        }

        for blob in [case] + findings:
            text = json.dumps(blob)
            indicators["ips"].update(re.findall(ip_pattern, text))
            indicators["hashes"].update(re.findall(hash_pattern, text))

            ctx = blob.get("entity_context", {})
            for key in ("src_ips", "dest_ips"):
                for ip in ctx.get(key, []):
                    indicators["ips"].add(str(ip))
            for u in ctx.get("usernames", []):
                indicators["usernames"].add(str(u))
            for h in ctx.get("hostnames", []):
                indicators["hostnames"].add(str(h))

        # Filter private IPs
        indicators["ips"] = {
            ip for ip in indicators["ips"] if not _is_private_ip(ip)
        }

        return {k: sorted(v) for k, v in indicators.items()}

    # ------------------------------------------------------------------
    # Elasticsearch queries
    # ------------------------------------------------------------------

    async def query_elastic_for_indicators(
        self,
        indicators: Dict[str, List[str]],
        hours: int = 168,
        index: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Query Elasticsearch for each indicator type."""
        enrichment: Dict[str, Any] = {
            "query_time": datetime.utcnow().isoformat(),
            "lookback_hours": hours,
            "results": {},
            "summary": {},
        }
        total = 0

        if indicators.get("ips"):
            enrichment["results"]["ips"] = {}
            for ip in indicators["ips"][:10]:
                result = await self.elastic_service.search_by_ip(
                    ip, index=index, hours=hours
                )
                if result:
                    hits = result.get("hits", {}).get("hits", [])[:100]
                    enrichment["results"]["ips"][ip] = hits
                    total += len(hits)

        if indicators.get("hashes"):
            enrichment["results"]["hashes"] = {}
            for h in indicators["hashes"][:10]:
                result = await self.elastic_service.search_by_hash(
                    h, index=index, hours=hours
                )
                if result:
                    hits = result.get("hits", {}).get("hits", [])[:100]
                    enrichment["results"]["hashes"][h] = hits
                    total += len(hits)

        if indicators.get("usernames"):
            enrichment["results"]["usernames"] = {}
            for u in indicators["usernames"][:10]:
                result = await self.elastic_service.search_by_username(
                    u, index=index, hours=hours
                )
                if result:
                    hits = result.get("hits", {}).get("hits", [])[:100]
                    enrichment["results"]["usernames"][u] = hits
                    total += len(hits)

        if indicators.get("hostnames"):
            enrichment["results"]["hostnames"] = {}
            for h in indicators["hostnames"][:10]:
                result = await self.elastic_service.search_by_hostname(
                    h, index=index, hours=hours
                )
                if result:
                    hits = result.get("hits", {}).get("hits", [])[:100]
                    enrichment["results"]["hostnames"][h] = hits
                    total += len(hits)

        enrichment["summary"] = {
            "total_events": total,
            "ips_queried": len(indicators.get("ips", [])),
            "hashes_queried": len(indicators.get("hashes", [])),
            "usernames_queried": len(indicators.get("usernames", [])),
            "hostnames_queried": len(indicators.get("hostnames", [])),
        }
        return enrichment

    # ------------------------------------------------------------------
    # Case enrichment
    # ------------------------------------------------------------------

    async def enrich_case(
        self, case_id: str, lookback_hours: int = 168
    ) -> Dict[str, Any]:
        """Enrich a case with Elasticsearch data."""
        case = self.data_service.get_case(case_id)
        if not case:
            return {"success": False, "error": f"Case {case_id} not found"}

        findings = []
        for fid in case.get("finding_ids", []):
            f = self.data_service.get_finding(fid)
            if f:
                findings.append(f)

        indicators = self.extract_indicators(case, findings)
        enrichment = await self.query_elastic_for_indicators(
            indicators, hours=lookback_hours
        )

        return {
            "success": True,
            "case_id": case_id,
            "enrichment_timestamp": datetime.utcnow().isoformat(),
            "indicators": {k: v for k, v in indicators.items() if v},
            "elastic_data": enrichment,
        }


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

def _is_private_ip(ip: str) -> bool:
    try:
        parts = [int(p) for p in ip.split(".")]
        if len(parts) != 4:
            return True
        if parts[0] == 10:
            return True
        if parts[0] == 172 and 16 <= parts[1] <= 31:
            return True
        if parts[0] == 192 and parts[1] == 168:
            return True
        if parts[0] == 127:
            return True
        return False
    except Exception:
        return True
