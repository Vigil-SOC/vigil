"""AI processing pipeline for finding triage and enrichment."""

import asyncio
import logging
from datetime import datetime
from typing import Optional, Dict, List, Any

from daemon.config import ProcessingConfig

logger = logging.getLogger(__name__)


class FindingProcessor:
    """Processes findings through AI triage and enrichment."""

    def __init__(self, config: ProcessingConfig):
        self.config = config
        self.input_queue: asyncio.Queue = asyncio.Queue()
        self._response_queue: Optional[asyncio.Queue] = None
        self._investigation_queue: Optional[asyncio.Queue] = None

        # Services (lazy loaded)
        self._data_service = None
        self._claude_service = None
        self._enrichment_services = {}
        self._sandbox_submitter = None

        # Processing semaphore
        self._semaphore = asyncio.Semaphore(config.max_concurrent_tasks)

        # Stats
        self.stats = {
            "processed": 0,
            "triaged": 0,
            "enriched": 0,
            "errors": 0,
            "queued_for_response": 0,
            "queued_for_investigation": 0,
            "sanitization_flagged": 0,
        }

    @staticmethod
    def _sanitize_finding(finding: Dict[str, Any], source: Optional[str]) -> None:
        """Issue #87: scan finding text for prompt-injection patterns before
        the content is rendered into a triage prompt.

        Detect-only in v1 — we log + count, but don't drop or rewrite the
        content. Source providers (Splunk, CrowdStrike, Elastic) are tagged
        so the metric is sliceable downstream.
        """
        try:
            from services.prompt_security import scan_for_injection
        except Exception:  # noqa: BLE001 — daemon must never crash on a hook
            return

        finding_id = finding.get("finding_id") or "unknown"
        description = finding.get("description") or ""
        entity_context = finding.get("entity_context") or {}
        # Stringify entity_context for the scan; keys are bounded/known.
        entity_blob = (
            " ".join(str(v) for v in entity_context.values() if v is not None)
            if isinstance(entity_context, dict)
            else str(entity_context)
        )

        patterns: List[str] = []
        patterns.extend(scan_for_injection(description).patterns)
        patterns.extend(scan_for_injection(entity_blob).patterns)
        if not patterns:
            return

        logger.warning(
            "finding sanitization flagged",
            extra={
                "event": "finding.sanitization.flagged",
                "finding_id": finding_id,
                "source": source or finding.get("data_source") or "unknown",
                "patterns": sorted(set(patterns)),
            },
        )

    def set_response_queue(self, queue: asyncio.Queue):
        """Set the queue for findings requiring response."""
        self._response_queue = queue

    def set_investigation_queue(self, queue: asyncio.Queue):
        """Set the queue for findings requiring autonomous investigation."""
        self._investigation_queue = queue

    def _init_services(self):
        """Initialize required services."""
        try:
            from services.database_data_service import DatabaseDataService

            self._data_service = DatabaseDataService()
            logger.info("Database service initialized")
        except Exception as e:
            logger.error(f"Failed to initialize database service: {e}")

        if self.config.auto_triage_enabled:
            self._llm_gateway = None  # lazy-init in async context

        if self.config.auto_enrich_enabled:
            self._init_enrichment_services()

    def _init_enrichment_services(self):
        """Initialize threat intelligence enrichment services."""
        from core.config import is_integration_enabled, get_integration_config

        # VirusTotal
        if is_integration_enabled("virustotal"):
            try:
                config = get_integration_config("virustotal")
                self._enrichment_services["virustotal"] = {
                    "api_key": config.get("api_key"),
                    "enabled": True,
                }
                logger.info("VirusTotal enrichment enabled")
            except Exception as e:
                logger.warning(f"VirusTotal not available: {e}")

        # Shodan
        if is_integration_enabled("shodan"):
            try:
                config = get_integration_config("shodan")
                self._enrichment_services["shodan"] = {
                    "api_key": config.get("api_key"),
                    "enabled": True,
                }
                logger.info("Shodan enrichment enabled")
            except Exception as e:
                logger.warning(f"Shodan not available: {e}")

        # Sandbox auto-submission (opt-in, disabled by default)
        try:
            from daemon.sandbox_submitter import SandboxSubmitter

            submitter = SandboxSubmitter()
            if submitter.enabled():
                self._sandbox_submitter = submitter
                logger.info("Sandbox auto-submission enabled")
            else:
                logger.debug(
                    "Sandbox auto-submission disabled (SANDBOX_AUTO_SUBMIT=false or no sandbox enabled)"
                )
        except Exception as e:
            logger.warning(f"Sandbox submitter not available: {e}")

    async def run(self, shutdown_event: asyncio.Event):
        """Run the processing loop."""
        logger.info("Finding processor starting...")
        self._init_services()

        # Start worker tasks
        workers = [
            asyncio.create_task(self._process_worker(i, shutdown_event))
            for i in range(self.config.max_concurrent_tasks)
        ]

        # Wait for shutdown
        await shutdown_event.wait()

        # Cancel workers
        for worker in workers:
            worker.cancel()

        await asyncio.gather(*workers, return_exceptions=True)
        logger.info("Finding processor stopped")

    async def _process_worker(self, worker_id: int, shutdown_event: asyncio.Event):
        """Worker coroutine for processing findings."""
        logger.debug(f"Processing worker {worker_id} started")

        while not shutdown_event.is_set():
            try:
                # Get item with timeout to allow shutdown checks
                try:
                    item = await asyncio.wait_for(self.input_queue.get(), timeout=1.0)
                except asyncio.TimeoutError:
                    continue

                async with self._semaphore:
                    await self._process_item(item)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Worker {worker_id} error: {e}")
                self.stats["errors"] += 1

    async def _process_item(self, item: Dict[str, Any]):
        """Process a single item from the queue."""
        item_type = item.get("type")

        if item_type == "finding":
            await self._process_finding(item["data"], item.get("source"))
        else:
            logger.warning(f"Unknown item type: {item_type}")

    async def _process_finding(
        self, finding: Dict[str, Any], source: Optional[str] = None
    ):
        """Process a finding through triage and enrichment."""
        finding_id = finding.get("finding_id", "unknown")
        logger.debug(f"Processing finding {finding_id} from {source}")

        try:
            # Issue #87: scan ingested finding for prompt-injection patterns
            # before any of its content reaches the LLM. Detect-only in v1.
            try:
                from services.prompt_security import scan_for_injection

                desc_patterns = scan_for_injection(
                    finding.get("description") or ""
                ).patterns
                ec = finding.get("entity_context") or {}
                ec_blob = (
                    " ".join(str(v) for v in ec.values() if v is not None)
                    if isinstance(ec, dict)
                    else str(ec)
                )
                ec_patterns = scan_for_injection(ec_blob).patterns
                if desc_patterns or ec_patterns:
                    self.stats["sanitization_flagged"] += 1
                    self._sanitize_finding(finding, source)
            except Exception as e:  # noqa: BLE001
                logger.debug(f"Sanitization hook error (non-fatal): {e}")

            # Store finding first
            if self._data_service:
                await self._store_finding(finding)

            # AI Triage (routed through LLM queue)
            if self.config.auto_triage_enabled:
                finding = await self._triage_finding(finding)

            # Enrichment
            if self.config.auto_enrich_enabled:
                finding = await self._enrich_finding(finding)

            # Update stored finding with enrichments
            if self._data_service:
                await self._update_finding(finding)

            # Check if response is needed
            await self._evaluate_for_response(finding)

            self.stats["processed"] += 1
            logger.info(
                f"Processed finding {finding_id} (severity: {finding.get('severity')})"
            )

        except Exception as e:
            logger.error(f"Error processing finding {finding_id}: {e}")
            self.stats["errors"] += 1

    async def _store_finding(self, finding: Dict[str, Any]):
        """Store finding in database."""
        try:
            from services.ingestion_service import IngestionService

            ingestion = IngestionService()
            ingestion.ingest_finding(finding)
        except Exception as e:
            logger.error(f"Failed to store finding: {e}")

    async def _update_finding(self, finding: Dict[str, Any]):
        """Update finding in database."""
        try:
            finding_id = finding.get("finding_id")
            if finding_id and self._data_service:
                self._data_service.update_finding(finding_id, finding)
        except Exception as e:
            logger.error(f"Failed to update finding: {e}")

    async def _triage_finding(self, finding: Dict[str, Any]) -> Dict[str, Any]:
        """Use AI to triage and classify finding."""

        try:
            # Build triage prompt
            prompt = self._build_triage_prompt(finding)

            # Get AI assessment with timeout
            response = await asyncio.wait_for(
                self._get_ai_triage(prompt), timeout=self.config.triage_timeout
            )

            if response:
                # Parse and apply AI assessment
                finding = self._apply_triage_result(finding, response)
                self.stats["triaged"] += 1

        except asyncio.TimeoutError:
            logger.warning(f"AI triage timed out for {finding.get('finding_id')}")
        except Exception as e:
            logger.error(f"AI triage error: {e}")

        return finding

    def _build_triage_prompt(self, finding: Dict[str, Any]) -> str:
        """Build prompt for AI triage."""
        entity_context = finding.get("entity_context") or {}
        mitre = finding.get("mitre_predictions") or {}
        desc = finding.get("description") or "N/A"

        src_ips = entity_context.get("src_ips") or []
        if not src_ips and entity_context.get("src_ip"):
            src_ips = [entity_context["src_ip"]]
        hostnames = entity_context.get("hostnames") or []
        if not hostnames and entity_context.get("hostname"):
            hostnames = [entity_context["hostname"]]
        users = entity_context.get("usernames") or entity_context.get("users") or []
        if not users and entity_context.get("user"):
            users = [entity_context["user"]]

        return f"""Analyze this security finding and provide a triage assessment:

Finding ID: {finding.get('finding_id') or 'N/A'}
Source: {finding.get('data_source') or 'unknown'}
Current Severity: {finding.get('severity') or 'unknown'}
Title: {finding.get('title') or 'N/A'}
Description: {desc[:500]}

Entity Context:
- Source IPs: {src_ips}
- Hostnames: {hostnames}
- Users: {users}

MITRE Predictions: {list(mitre.keys()) if mitre else 'None'}

Provide your assessment in the following format:
SEVERITY: [critical/high/medium/low]
CONFIDENCE: [0.0-1.0]
CATEGORY: [malware/intrusion/data_exfil/credential_theft/lateral_movement/other]
RECOMMENDED_ACTION: [isolate/block/investigate/monitor/dismiss]
REASONING: [Brief explanation]
"""

    async def _ensure_gateway(self):
        """Lazily initialise the LLM gateway (needs an event loop)."""
        if getattr(self, "_llm_gateway", None) is None:
            try:
                from services.llm_gateway import get_llm_gateway

                self._llm_gateway = await get_llm_gateway()
                logger.info("LLM gateway connected for AI triage")
            except Exception as e:
                logger.warning(f"Failed to connect LLM gateway: {e}")
                self._llm_gateway = None

    async def _get_ai_triage(self, prompt: str) -> Optional[str]:
        """Get AI triage response via the LLM queue."""
        await self._ensure_gateway()
        if self._llm_gateway is None:
            logger.warning("LLM gateway unavailable, skipping AI triage")
            return None
        try:
            result = await self._llm_gateway.submit_triage(prompt)
            if result is None:
                return None
            if isinstance(result, dict):
                return result.get("content", "")
            return str(result)
        except Exception as e:
            logger.error(f"LLM queue triage error: {e}")
            return None

    def _apply_triage_result(
        self, finding: Dict[str, Any], response: str
    ) -> Dict[str, Any]:
        """Apply AI triage result to finding."""
        # Parse response
        lines = response.strip().split("\n")
        triage_result = {}

        for line in lines:
            if ":" in line:
                key, value = line.split(":", 1)
                key = key.strip().upper()
                value = value.strip()

                if key == "SEVERITY":
                    severity = value.lower()
                    if severity in ["critical", "high", "medium", "low"]:
                        finding["severity"] = severity
                        triage_result["severity"] = severity

                elif key == "CONFIDENCE":
                    try:
                        confidence = float(value)
                        triage_result["confidence"] = confidence
                        finding["triage_confidence"] = confidence
                    except ValueError:
                        pass

                elif key == "CATEGORY":
                    triage_result["category"] = value.lower()
                    finding["category"] = value.lower()

                elif key == "RECOMMENDED_ACTION":
                    triage_result["recommended_action"] = value.lower()
                    finding["recommended_action"] = value.lower()

                elif key == "REASONING":
                    triage_result["reasoning"] = value
                    finding["triage_reasoning"] = value

        # Add triage metadata
        finding["ai_triage"] = {
            "timestamp": datetime.utcnow().isoformat(),
            "result": triage_result,
        }

        return finding

    async def _enrich_finding(self, finding: Dict[str, Any]) -> Dict[str, Any]:
        """Enrich finding with threat intelligence."""
        entity_context = finding.get("entity_context") or {}
        enrichment = {}

        # Enrich IPs (handle both singular and plural field formats)
        src_ips = entity_context.get("src_ips") or []
        if not src_ips and entity_context.get("src_ip"):
            src_ips = [entity_context["src_ip"]]
        dst_ips = entity_context.get("dest_ips") or entity_context.get("dst_ips") or []
        if not dst_ips and entity_context.get("dst_ip"):
            dst_ips = [entity_context["dst_ip"]]
        ips = src_ips + dst_ips
        for ip in ips[:5]:  # Limit to 5 IPs
            ip_enrichment = await self._enrich_ip(ip)
            if ip_enrichment:
                enrichment[f"ip_{ip}"] = ip_enrichment

        # Enrich hashes if present
        hashes = entity_context.get("file_hashes", [])
        for hash_val in hashes[:3]:  # Limit to 3 hashes
            hash_enrichment = await self._enrich_hash(hash_val)
            if hash_enrichment:
                enrichment[f"hash_{hash_val[:16]}"] = hash_enrichment

        # Opt-in sandbox auto-submission (see daemon/sandbox_submitter.py)
        if self._sandbox_submitter is not None and hashes:
            file_hint = {
                "file_name": (
                    (entity_context.get("file_names") or [None])[0]
                    if isinstance(entity_context.get("file_names"), list)
                    else entity_context.get("file_name")
                ),
                "file_size": entity_context.get("file_size"),
            }
            submissions: Dict[str, Any] = {}
            for hash_val in hashes[:3]:
                try:
                    res = await self._sandbox_submitter.submit_hash(hash_val, file_hint)
                    if res and res.get("status") not in ("disabled", "rejected"):
                        submissions[hash_val] = res
                except Exception as e:
                    logger.debug(f"Sandbox submission failed for {hash_val}: {e}")
            if submissions:
                enrichment["sandbox_submissions"] = submissions

        if enrichment:
            finding["enrichment"] = enrichment
            finding["enriched_at"] = datetime.utcnow().isoformat()
            self.stats["enriched"] += 1

        return finding

    async def _enrich_ip(self, ip: str) -> Optional[Dict[str, Any]]:
        """Enrich IP address with threat intel."""
        result = {}

        # Shodan lookup
        if self._enrichment_services.get("shodan", {}).get("enabled"):
            try:
                import requests

                api_key = self._enrichment_services["shodan"]["api_key"]
                resp = await asyncio.to_thread(
                    requests.get,
                    f"https://api.shodan.io/shodan/host/{ip}",
                    params={"key": api_key},
                    timeout=10,
                )
                if resp.status_code == 200:
                    data = resp.json()
                    result["shodan"] = {
                        "ports": data.get("ports", []),
                        "hostnames": data.get("hostnames", []),
                        "org": data.get("org"),
                        "isp": data.get("isp"),
                        "vulns": data.get("vulns", []),
                    }
            except Exception as e:
                logger.debug(f"Shodan lookup failed for {ip}: {e}")

        # VirusTotal IP lookup
        if self._enrichment_services.get("virustotal", {}).get("enabled"):
            try:
                import requests

                api_key = self._enrichment_services["virustotal"]["api_key"]
                resp = await asyncio.to_thread(
                    requests.get,
                    f"https://www.virustotal.com/api/v3/ip_addresses/{ip}",
                    headers={"x-apikey": api_key},
                    timeout=10,
                )
                if resp.status_code == 200:
                    data = resp.json().get("data", {}).get("attributes", {})
                    stats = data.get("last_analysis_stats", {})
                    result["virustotal"] = {
                        "malicious": stats.get("malicious", 0),
                        "suspicious": stats.get("suspicious", 0),
                        "harmless": stats.get("harmless", 0),
                        "reputation": data.get("reputation", 0),
                    }
            except Exception as e:
                logger.debug(f"VirusTotal lookup failed for {ip}: {e}")

        return result if result else None

    async def _enrich_hash(self, hash_val: str) -> Optional[Dict[str, Any]]:
        """Enrich file hash with threat intel."""
        if not self._enrichment_services.get("virustotal", {}).get("enabled"):
            return None

        try:
            import requests

            api_key = self._enrichment_services["virustotal"]["api_key"]
            resp = await asyncio.to_thread(
                requests.get,
                f"https://www.virustotal.com/api/v3/files/{hash_val}",
                headers={"x-apikey": api_key},
                timeout=10,
            )
            if resp.status_code == 200:
                data = resp.json().get("data", {}).get("attributes", {})
                stats = data.get("last_analysis_stats", {})
                return {
                    "malicious": stats.get("malicious", 0),
                    "suspicious": stats.get("suspicious", 0),
                    "harmless": stats.get("harmless", 0),
                    "type": data.get("type_description"),
                    "names": data.get("names", [])[:5],
                }
        except Exception as e:
            logger.debug(f"VirusTotal hash lookup failed: {e}")

        return None

    async def _evaluate_for_response(self, finding: Dict[str, Any]):
        """Evaluate if finding needs autonomous response."""
        severity = finding.get("severity", "").lower()
        recommended_action = finding.get("recommended_action", "").lower()
        confidence = finding.get("triage_confidence", 0.5)

        # Queue for response if high severity or action recommended
        should_respond = (
            severity in ["critical", "high"]
            or recommended_action in ["isolate", "block"]
            or confidence >= 0.85
        )

        if should_respond and self._response_queue:
            await self._response_queue.put(
                {
                    "type": "response_candidate",
                    "finding": finding,
                    "timestamp": datetime.utcnow().isoformat(),
                }
            )
            self.stats["queued_for_response"] += 1
            logger.info(
                f"Finding {finding.get('finding_id')} queued for response evaluation"
            )

        if should_respond and self._investigation_queue:
            await self._investigation_queue.put(
                {
                    "type": "finding",
                    "data": finding,
                    "timestamp": datetime.utcnow().isoformat(),
                }
            )
            self.stats["queued_for_investigation"] += 1
            logger.info(
                f"Finding {finding.get('finding_id')} queued for autonomous investigation"
            )
