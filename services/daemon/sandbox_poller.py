"""Polls pending sandbox submissions and correlates completed reports.

The daemon's enrichment step (``daemon/processor.py``) records
``finding.ai_enrichment.enrichment.sandbox_submissions`` with task IDs per
sandbox — the payload is nested one level inside the column. Those
tasks take minutes to complete — so a separate poller checks them on a
cadence, pulls the report when ready, and writes it back to the finding
plus (if the finding is tied to a case) the case as evidence + IOCs.

All HTTP is wrapped with ``asyncio.to_thread`` so the scheduler loop stays
async. DB writes go through the same pattern.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
from typing import Any, Dict, Optional

import httpx

from core.config import get_integration_config, get_settings
from core.secrets import get_secret
from core.time import utcnow
from services.daemon.vendor_errors import (
    AUTH_STATUSES,
    note_response,
    vendor_cooling_down,
)

logger = logging.getLogger(__name__)

# Terminal sub statuses: never re-polled.
_FINAL_STATUSES = ("failed", "expired", "reported")


class SandboxAuthError(Exception):
    """The sandbox rejected our credentials (401/403) while polling."""

    def __init__(self, vendor: str, status: int) -> None:
        super().__init__(f"{vendor} rejected the API key (HTTP {status})")
        self.vendor = vendor
        self.status = status


def _check(vendor: str, resp: Any) -> None:
    """Record auth/quota rejections; raise on auth, treat 429 as not ready."""
    if note_response(vendor, resp) and resp.status_code in AUTH_STATUSES:
        raise SandboxAuthError(vendor, resp.status_code)


class SandboxPoller:
    def __init__(self, data_service: Any = None) -> None:
        self._data_service = data_service
        self._correlation = None
        self._timeout_seconds = get_settings().sandbox_analysis_timeout

    def _init_services(self) -> None:
        if self._data_service is None:
            try:
                from core.storage.database_data_service import DatabaseDataService

                self._data_service = DatabaseDataService()
            except Exception as e:
                logger.warning(f"Sandbox poller could not init data service: {e}")
        if self._correlation is None:
            try:
                from core.cases.sandbox_correlation_service import (
                    SandboxCorrelationService,
                )

                self._correlation = SandboxCorrelationService()
            except Exception as e:
                logger.warning(f"Sandbox correlation service unavailable: {e}")

    async def run_once(self) -> Dict[str, int]:
        """Scan recent findings, advance any pending sandbox submissions."""
        self._init_services()
        if not self._data_service:
            return {
                "checked": 0,
                "completed": 0,
                "expired": 0,
                "failed": 0,
                "errors": 0,
            }

        try:
            findings = await asyncio.to_thread(self._data_service.get_findings)
        except TypeError:
            findings = self._data_service.get_findings()
        except Exception as e:
            logger.error(f"Failed to list findings for sandbox poll: {e}")
            return {
                "checked": 0,
                "completed": 0,
                "expired": 0,
                "failed": 0,
                "errors": 1,
            }

        stats = {"checked": 0, "completed": 0, "expired": 0, "failed": 0, "errors": 0}

        for finding in findings or []:
            # processor.py nests its enrichment payload under the ai_enrichment
            # column: ai_enrichment["enrichment"]["sandbox_submissions"].
            ai_enrichment = finding.get("ai_enrichment") or {}
            enrichment = ai_enrichment.get("enrichment") or {}
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

                    # Skip if we already have a report for this (hash, sandbox)
                    report_key = f"{hash_val}:{sandbox_name}"
                    if report_key in reports or sub.get("status") in _FINAL_STATUSES:
                        continue
                    stats["checked"] += 1

                    # Enforce timeout
                    if self._is_expired(sub):
                        sub["status"] = "expired"
                        stats["expired"] += 1
                        updated = True
                        continue

                    try:
                        report = await self._fetch_report(sandbox_name, task_id)
                    except SandboxAuthError as e:
                        sub["status"] = "failed"
                        sub["error"] = f"http_{e.status}"
                        stats["failed"] += 1
                        updated = True
                        continue
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
                        "fetched_at": utcnow().isoformat(),
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
                # Merge back into the column the payload came from; writing a
                # bare `enrichment=` kwarg is dropped as an unknown field.
                persisted = await asyncio.to_thread(
                    self._data_service.update_finding,
                    finding.get("finding_id"),
                    ai_enrichment={**ai_enrichment, "enrichment": enrichment},
                )
                if not persisted:
                    logger.error(
                        "Failed to persist sandbox reports on finding %s",
                        finding.get("finding_id"),
                    )
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
        base = get_settings().cape_sandbox_url.rstrip("/")
        api_key = get_secret("CAPE_SANDBOX_API_KEY") or ""
        if not base or vendor_cooling_down("cape"):
            return None
        headers = {"Authorization": f"Token {api_key}"} if api_key else {}
        status_resp = await asyncio.to_thread(
            httpx.get,
            f"{base}/apiv2/tasks/status/{task_id}/",
            headers=headers,
            timeout=15,
            follow_redirects=True,
        )
        _check("cape", status_resp)
        if status_resp.status_code != 200:
            return None
        status_data = status_resp.json()
        status = (status_data.get("data") or status_data).get("status")
        if status != "reported":
            return None
        report_resp = await asyncio.to_thread(
            httpx.get,
            f"{base}/apiv2/tasks/get/report/{task_id}/",
            headers=headers,
            timeout=60,
            follow_redirects=True,
        )
        _check("cape", report_resp)
        if report_resp.status_code == 200:
            return report_resp.json()
        return None

    async def _fetch_hybrid(self, task_id: str) -> Optional[Dict[str, Any]]:
        cfg = get_integration_config("hybrid_analysis") or {}
        api_key = cfg.get("api_key") or get_secret("HYBRID_ANALYSIS_API_KEY") or ""
        if not api_key or vendor_cooling_down("hybrid_analysis"):
            return None
        resp = await asyncio.to_thread(
            httpx.get,
            f"https://www.hybrid-analysis.com/api/v2/report/{task_id}/summary",
            headers={"api-key": api_key, "User-Agent": "Falcon Sandbox"},
            timeout=30,
            follow_redirects=True,
        )
        _check("hybrid_analysis", resp)
        if resp.status_code == 200:
            data = resp.json()
            # Hybrid Analysis returns state=SUCCESS when done
            if str(data.get("state", "")).upper() == "SUCCESS":
                return data
        return None

    async def _fetch_anyrun(self, task_id: str) -> Optional[Dict[str, Any]]:
        cfg = get_integration_config("anyrun") or {}
        api_key = cfg.get("api_key") or get_secret("ANYRUN_API_KEY") or ""
        if not api_key or vendor_cooling_down("anyrun"):
            return None
        resp = await asyncio.to_thread(
            httpx.get,
            f"https://api.any.run/v1/analysis/{task_id}",
            headers={"Authorization": f"API-Key {api_key}"},
            timeout=30,
            follow_redirects=True,
        )
        _check("anyrun", resp)
        if resp.status_code == 200:
            data = resp.json()
            if str(data.get("status", "")).lower() == "done":
                return data
        return None

    async def _fetch_joe(self, task_id: str) -> Optional[Dict[str, Any]]:
        api_key = get_secret("JOE_SANDBOX_API_KEY") or get_secret("JBXAPIKEY") or ""
        base = get_settings().joe_sandbox_url.rstrip("/")
        if not api_key or vendor_cooling_down("joe_sandbox"):
            return None
        resp = await asyncio.to_thread(
            httpx.post,
            f"{base}/v2/analysis/info",
            data={"apikey": api_key, "webid": task_id},
            timeout=30,
            follow_redirects=True,
        )
        _check("joe_sandbox", resp)
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
        return utcnow() - submitted > timedelta(seconds=self._timeout_seconds)
