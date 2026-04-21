"""Shared intelligence layer for cross-investigation correlation.

Maintains a centralized IOC index so the orchestrator can detect
overlapping investigations and link related cases.
"""

import logging
import os
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

logger = logging.getLogger(__name__)


def _get_mempalace_searcher():
    """Return a MemPalace Searcher handle for cross-run lookups, or None if unavailable."""
    if os.environ.get("MEMPALACE_DAEMON_ENABLED", "false").lower() != "true":
        return None
    try:
        from mempalace.searcher import search_memories

        data_dir = Path(
            os.environ.get(
                "MEMPALACE_PALACE_PATH", str(Path.home() / ".mempalace" / "palace")
            )
        )
        return (search_memories, data_dir)
    except Exception as e:
        logger.debug(f"MemPalace searcher unavailable in daemon: {e}")
        return None


class SharedIntelligence:
    """In-memory cross-investigation IOC and entity tracker.

    Backed by MemPalace for cross-run persistence (when MEMPALACE_DAEMON_ENABLED=true),
    with an in-memory cache for fast lookups during the orchestrator loop.
    """

    def __init__(self):
        self._ioc_index: Dict[str, Set[str]] = defaultdict(set)
        self._entity_index: Dict[str, Set[str]] = defaultdict(set)
        self._investigation_iocs: Dict[str, Set[str]] = defaultdict(set)
        self._mp = _get_mempalace_searcher()  # (search_fn, palace_path) or None

    def register_entities(self, investigation_id: str, finding: Dict[str, Any]):
        """Extract and register entities from a finding for dedup checks."""
        ctx = finding.get("entity_context") or {}

        for ip in ctx.get("src_ips") or []:
            self._register(investigation_id, "ip", ip)
        if ctx.get("src_ip"):
            self._register(investigation_id, "ip", ctx["src_ip"])

        for ip in ctx.get("dest_ips") or ctx.get("dst_ips") or []:
            self._register(investigation_id, "ip", ip)
        if ctx.get("dst_ip"):
            self._register(investigation_id, "ip", ctx["dst_ip"])

        for host in ctx.get("hostnames") or []:
            self._register(investigation_id, "hostname", host)
        if ctx.get("hostname"):
            self._register(investigation_id, "hostname", ctx["hostname"])

        for user in ctx.get("usernames") or ctx.get("users") or []:
            self._register(investigation_id, "user", user)
        if ctx.get("user"):
            self._register(investigation_id, "user", ctx["user"])

        for h in ctx.get("file_hashes") or []:
            self._register(investigation_id, "hash", h)

        for d in ctx.get("domains") or []:
            self._register(investigation_id, "domain", d)

    def register_iocs(self, investigation_id: str, iocs: List[Dict[str, str]]):
        """Register IOCs discovered during an investigation."""
        for ioc in iocs:
            ioc_type = ioc.get("type", "unknown")
            value = ioc.get("value", "")
            if value:
                self._register(investigation_id, ioc_type, value)

    def check_overlap(
        self, finding: Dict[str, Any], exclude_id: Optional[str] = None
    ) -> List[str]:
        """Check if a finding's entities overlap with any active or recent investigation.

        Returns list of investigation_ids that share entities.
        Checks in-memory index first (active run), then MemPalace for cross-run history.
        """
        ctx = finding.get("entity_context") or {}
        overlapping = set()

        all_values = set()
        for ip in ctx.get("src_ips") or []:
            all_values.add(f"ip:{ip}")
        if ctx.get("src_ip"):
            all_values.add(f"ip:{ctx['src_ip']}")
        for ip in ctx.get("dest_ips") or ctx.get("dst_ips") or []:
            all_values.add(f"ip:{ip}")
        if ctx.get("dst_ip"):
            all_values.add(f"ip:{ctx['dst_ip']}")
        for host in ctx.get("hostnames") or []:
            all_values.add(f"hostname:{host}")
        if ctx.get("hostname"):
            all_values.add(f"hostname:{ctx['hostname']}")
        for user in ctx.get("usernames") or ctx.get("users") or []:
            all_values.add(f"user:{user}")
        if ctx.get("user"):
            all_values.add(f"user:{ctx['user']}")
        for h in ctx.get("file_hashes") or []:
            all_values.add(f"hash:{h}")
        for d in ctx.get("domains") or []:
            all_values.add(f"domain:{d}")

        # L1: in-memory check (active investigations in this run)
        for key in all_values:
            overlapping.update(self._ioc_index.get(key, set()))

        # L2: MemPalace cross-run check via Searcher (closed investigations)
        if self._mp and all_values:
            try:
                search_fn, palace_path = self._mp
                query = " ".join(list(all_values)[:10])  # cap to avoid overlong queries
                results = search_fn(query=query, palace_path=str(palace_path))
                for result in results or []:
                    # Results are text snippets; look for investigation IDs in the text
                    text = str(result)
                    import re

                    for inv_id in re.findall(r"inv-[a-f0-9-]{8,}", text):
                        overlapping.add(f"historical:{inv_id}")
            except Exception as e:
                logger.debug(f"MemPalace cross-run overlap check failed: {e}")

        if exclude_id:
            overlapping.discard(exclude_id)

        return list(overlapping)

    def get_related_investigations(self, investigation_id: str) -> List[str]:
        """Get investigations that share IOCs with the given one."""
        my_keys = self._investigation_iocs.get(investigation_id, set())
        related = set()
        for key in my_keys:
            for inv_id in self._ioc_index.get(key, set()):
                if inv_id != investigation_id:
                    related.add(inv_id)
        return list(related)

    def get_shared_iocs(self, inv_id_a: str, inv_id_b: str) -> List[str]:
        """Get IOC keys shared between two investigations."""
        keys_a = self._investigation_iocs.get(inv_id_a, set())
        keys_b = self._investigation_iocs.get(inv_id_b, set())
        return list(keys_a & keys_b)

    def unregister_investigation(self, investigation_id: str):
        """Remove all IOC registrations for a completed/archived investigation."""
        keys = self._investigation_iocs.pop(investigation_id, set())
        for key in keys:
            self._ioc_index[key].discard(investigation_id)
            if not self._ioc_index[key]:
                del self._ioc_index[key]

    def get_stats(self) -> Dict[str, int]:
        return {
            "total_iocs_tracked": len(self._ioc_index),
            "total_investigations_tracked": len(self._investigation_iocs),
        }

    def _register(self, investigation_id: str, ioc_type: str, value: str):
        if not value or not value.strip():
            return
        key = f"{ioc_type}:{value.strip().lower()}"
        self._ioc_index[key].add(investigation_id)
        self._investigation_iocs[investigation_id].add(key)
