"""Polls pending sandbox submissions and correlates completed reports.

The daemon's enrichment step (``daemon/processor.py``) records
``finding.enrichment.sandbox_submissions`` with task IDs per sandbox. Those
tasks take minutes to complete — so a separate poller checks them on a
cadence, pulls the report when ready, and writes it back to the finding
plus (if the finding is tied to a case) the case as evidence + IOCs.

All HTTP is wrapped with ``asyncio.to_thread`` so the scheduler loop stays
async. DB writes go through the same pattern.
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timedelta
from typing import Any, Dict, Optional

import requests

logger = logging.getLogger(__name__)

_SANDBOX_TIMEOUT_DEFAULT = 300


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


class SandboxPoller:
    """Resolve pending sandbox submissions into reports + IOCs."""

    def __init__(self, data_service: Any = None) -> None:
        self._data_service = data_service
        self._correlation = None
        self._timeout_seconds = _env_int(
            "SANDBOX_ANALYSIS_TIMEOUT", _SANDBOX_TIMEOUT_DEFAULT
        )

    def _init_services(self) -> None:
        if self._data_service is None:
            try:
                from services.database_data_service import DatabaseDataService

                self._data_service = DatabaseDataService()
            except Exception as e:
                logger.warning(f"Sandbox poller could not init data service: {e}")
        if self._correlation is None:
            try:
                from services.sandbox_correlation_service import (
                    SandboxCorrelationService,
                )

                self._correlation = SandboxCorrelationService()
            except Exception as e:
                logger.warning(f"Sandbox correlation service unavailable: {e}")

    async def run_once(self) -> Dict[str, int]:
        """Scan recent findings, advance any pending sandbox submissions."""
        self._init_services()
        if not self._data_service:
            return {"checked": 0, "completed": 0, "expired": 0, "errors": 0}

        try:
            findings = await asyncio.to_thread(self._data_service.get_findings)
        except TypeError:
            findings = self._data_service.get_findings()
        except Exception as e:
            logger.error(f"Failed to list findings for sandbox poll: {e}")
            return {"checked": 0, "completed": 0, "expired": 0, "errors": 1}

        stats = {"checked": 0, "completed": 0, "expired": 0, "errors": 0}

        for finding in findings or []:
            enrichment = finding.get("enrichment") or {}
            pending = enrichment.get("sandbox_submissions") or {}
            reports = enrichment.get("sandbox_reports") or {}
            if not pending:
                continue

            updated = False
            for hash_val, per_sandbox in list(pending.items()):
                if not isinstance(per_sandbox, dict):
                    continue
                for sandbox_name, sub in list(per_sandbox.items()):
                    if not isinstance(sub, dict):
                        continue
                    task_id = sub.get("task_id")
                    if not task_id:
                        continue
                    stats["checked"] += 1

                    # Skip if we already have a report for this (hash, sandbox)
                    report_key = f"{hash_val}:{sandbox_name}"
                    if report_key in reports:
                        continue

                    # Enforce timeout
                    if self._is_expired(sub):
                        sub["status"] = "expired"
                        stats["expired"] += 1
                        updated = True
                        continue

                    try:
                        report = await self._fetch_report(sandbox_name, task_id)
                    except Exception as e:
                        logger.debug(
                            f"Fetch report failed for {sandbox_name}/{task_id}: {e}"
                        )
                        stats["errors"] += 1
                        continue

                    if not report:
                        continue

                    reports[report_key] = {
                        "sandbox": sandbox_name,
                        "task_id": task_id,
                        "fetched_at": datetime.utcnow().isoformat(),
                        "report": report,
                    }
                    sub["status"] = "reported"
                    stats["completed"] += 1
                    updated = True

                    case_id = finding.get("case_id")
                    if case_id and self._correlation:
                        try:
                            await asyncio.to_thread(
                                self._correlation.attach_report,
                                case_id,
                                sandbox_name,
                                str(task_id),
                                report,
                            )
                        except Exception as e:
                            logger.error(
                                f"Correlation failed for {sandbox_name}/{task_id}: {e}"
                            )

            if updated:
                enrichment["sandbox_submissions"] = pending
                enrichment["sandbox_reports"] = reports
                try:
                    await asyncio.to_thread(
                        self._data_service.update_finding,
                        finding.get("finding_id"),
                        enrichment=enrichment,
                    )
                except Exception as e:
                    logger.error(f"Failed to persist sandbox updates on finding: {e}")
                    stats["errors"] += 1

        return stats

    # ---------- per-sandbox fetch ----------

    async def _fetch_report(
        self, sandbox_name: str, task_id: str
    ) -> Optional[Dict[str, Any]]:
        name = sandbox_name.lower()
        if name in ("cape", "cape-sandbox", "cape_sandbox"):
            return await self._fetch_cape(task_id)
        if name in ("hybrid_analysis", "hybrid-analysis", "hybrid"):
            return await self._fetch_hybrid(task_id)
        if name in ("anyrun", "any.run"):
            return await self._fetch_anyrun(task_id)
        if name in ("joe", "joe_sandbox", "joe-sandbox"):
            return await self._fetch_joe(task_id)
        return None

    async def _fetch_cape(self, task_id: str) -> Optional[Dict[str, Any]]:
        base = os.getenv("CAPE_SANDBOX_URL", "").rstrip("/")
        api_key = os.getenv("CAPE_SANDBOX_API_KEY", "")
        if not base:
            return None
        headers = {"Authorization": f"Token {api_key}"} if api_key else {}
        status_resp = await asyncio.to_thread(
            requests.get,
            f"{base}/apiv2/tasks/status/{task_id}/",
            headers=headers,
            timeout=15,
        )
        if status_resp.status_code != 200:
            return None
        status_data = status_resp.json()
        status = (status_data.get("data") or status_data).get("status")
        if status != "reported":
            return None
        report_resp = await asyncio.to_thread(
            requests.get,
            f"{base}/apiv2/tasks/get/report/{task_id}/",
            headers=headers,
            timeout=60,
        )
        if report_resp.status_code == 200:
            return report_resp.json()
        return None

    async def _fetch_hybrid(self, task_id: str) -> Optional[Dict[str, Any]]:
        from core.config import get_integration_config

        cfg = get_integration_config("hybrid_analysis") or {}
        api_key = cfg.get("api_key") or os.getenv("HYBRID_ANALYSIS_API_KEY", "")
        if not api_key:
            return None
        resp = await asyncio.to_thread(
            requests.get,
            f"https://www.hybrid-analysis.com/api/v2/report/{task_id}/summary",
            headers={"api-key": api_key, "User-Agent": "Falcon Sandbox"},
            timeout=30,
        )
        if resp.status_code == 200:
            data = resp.json()
            # Hybrid Analysis returns state=SUCCESS when done
            if str(data.get("state", "")).upper() == "SUCCESS":
                return data
        return None

    async def _fetch_anyrun(self, task_id: str) -> Optional[Dict[str, Any]]:
        from core.config import get_integration_config

        cfg = get_integration_config("anyrun") or {}
        api_key = cfg.get("api_key") or os.getenv("ANYRUN_API_KEY", "")
        if not api_key:
            return None
        resp = await asyncio.to_thread(
            requests.get,
            f"https://api.any.run/v1/analysis/{task_id}",
            headers={"Authorization": f"API-Key {api_key}"},
            timeout=30,
        )
        if resp.status_code == 200:
            data = resp.json()
            if str(data.get("status", "")).lower() == "done":
                return data
        return None

    async def _fetch_joe(self, task_id: str) -> Optional[Dict[str, Any]]:
        api_key = os.getenv("JOE_SANDBOX_API_KEY", "") or os.getenv("JBXAPIKEY", "")
        base = os.getenv(
            "JOE_SANDBOX_URL", "https://jbxcloud.joesecurity.org/api"
        ).rstrip("/")
        if not api_key:
            return None
        resp = await asyncio.to_thread(
            requests.post,
            f"{base}/v2/analysis/info",
            data={"apikey": api_key, "webid": task_id},
            timeout=30,
        )
        if resp.status_code == 200:
            data = resp.json()
            if str(data.get("status", "")).lower() == "finished":
                return data
        return None

    # ---------- helpers ----------

    def _is_expired(self, sub: Dict[str, Any]) -> bool:
        ts = sub.get("submitted_at")
        if not ts:
            return False
        try:
            submitted = datetime.fromisoformat(ts)
        except ValueError:
            return False
        return datetime.utcnow() - submitted > timedelta(seconds=self._timeout_seconds)
