"""Data source polling for the SOC daemon.

Two modes coexist here:

* **Legacy per-source loops** (``_poll_<source>_loop``) — driven by env-var
  intervals in :class:`daemon.config.PollingConfig`. These predate federation
  and remain the path used when the global federation toggle is off.
* **Federation runner** (:class:`core.federation.runner.FederationRunner`) —
  spawned alongside the legacy loops. When ``federation.settings.enabled`` is
  true and a source has a ``federation_sources`` row enabled, the legacy loop
  for that source defers (skips that tick) so federation owns the pull.

This co-existence keeps existing deployments working unchanged while the new
opt-in feature is under MVP. Once federation is the default path we can
delete the legacy loops in a follow-up.
"""

import asyncio
import hmac
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Callable, Dict, Optional

from core.config import get_settings
from core.daemon_config import PollingConfig
from core.federation.runner import FederationRunner
from core.ingestion.dedup import RedisDedupSet
from core.integrations.crowdstrike.adapter import (
    _alert_to_finding,
    endpoint_alert_filter,
)
from core.integrations.splunk.adapter import _splunk_event_to_finding
from core.storage.push_health import DAEMON_SOURCE, note_dropped, note_success
from core.storage.service import _mitre_prediction_items
from core.time import utcnow
from core.webhook_rejections import (
    BAD_TOKEN,
    DISABLED,
    NO_SECRET,
    record_rejection,
    rejection_counts,
)

logger = logging.getLogger(__name__)


class IngestionError(RuntimeError):
    """An ingestion service reported success=False for a poll."""


def normalize_mitre_predictions(
    raw: Any,
    finding_id: str,
    data_source: Optional[str] = None,
) -> Dict[str, Optional[float]]:
    """Coerce a webhook ``mitre_predictions`` value to ``{technique_id: score}``.

    Accepted: a dict, a list or tuple of technique ids, a list of
    ``{"technique"|"id": ..., "confidence"|"score": ...}`` dicts, a single id
    string, or None. A missing score is null. Numbers and numeric strings keep
    their value; a bool or other string is null. Any other raw type, or a list
    entry with no technique id, raises ValueError so the request fails with a
    400 rather than the field being silently emptied downstream.
    """
    if raw is None:
        return {}
    if isinstance(raw, dict):
        shaped = raw
    else:
        if isinstance(raw, str):
            entries: list = [raw]
        elif isinstance(raw, (list, tuple)):
            entries = list(raw)
        else:
            raise ValueError(
                f"finding {finding_id}: mitre_predictions must be a dict, list or "
                f"string, got {type(raw).__name__}"
            )
        shaped = {}
        for item in entries:
            score: Any = None
            if isinstance(item, dict):
                # `or` rather than .get(default): a present-but-null key falls through.
                technique = item.get("technique") or item.get("id")
                score = item.get("confidence")
                if score is None:
                    score = item.get("score")
            else:
                technique = item
            if not isinstance(technique, str) or not technique.strip():
                raise ValueError(
                    f"finding {finding_id}: mitre_predictions entry {item!r} has no "
                    "technique id"
                )
            shaped[technique.strip()] = score
    return dict(
        _mitre_prediction_items(shaped, finding_id=finding_id, data_source=data_source)
    )


@dataclass
class PollState:
    """Per-source polling cursor.

    Deduplication lives in ``RedisDedupSet`` (see ``DataPoller``);
    this dataclass tracks only the last-poll timestamp used to compute
    query windows.
    """

    last_poll_time: Optional[datetime] = None

    def lookback_minutes(self, interval_seconds: int) -> int:
        # After a successful poll, look back to that timestamp (rounded up one
        # minute, same as SplunkAdapter.fetch) so an outage is re-queried.
        # Cap at 60 minutes: an uncapped window can exceed the ~110s job deadline,
        # fail, and then retry the same window forever. First run keeps the
        # fixed interval window. Never narrower than that window: sources index
        # detections a minute or more after creation, so a poll one tick after
        # the last success must still reach back past the previous poll.
        floor = max(interval_seconds // 60 + 1, 5)
        if self.last_poll_time is None:
            return floor
        elapsed = int((utcnow() - self.last_poll_time).total_seconds() // 60) + 1
        return min(max(elapsed, floor), 60)


class DataPoller:
    """Polls various data sources for new security findings."""

    def __init__(self, config: PollingConfig):
        self.config = config
        self._output_queue: Optional[asyncio.Queue] = None

        # Federation runner — owns pull for sources with a federation_sources
        # row when the global federation.settings toggle is on. Always
        # constructed; idle while federation is off.
        self._federation = FederationRunner(output_queue=None)

        # Polling cursors for each source
        self._splunk_state = PollState()
        self._crowdstrike_state = PollState()
        self._azure_sentinel_state = PollState()
        self._aws_security_hub_state = PollState()
        self._microsoft_defender_state = PollState()
        self._elastic_state = PollState()
        self._opensearch_state = PollState()

        # Durable per-source dedup sets (Redis-backed)
        self._splunk_dedup = RedisDedupSet("poller:splunk")
        self._crowdstrike_dedup = RedisDedupSet("poller:crowdstrike")
        self._elastic_dedup = RedisDedupSet("poller:elastic")
        self._webhook_dedup = RedisDedupSet("poller:webhook")
        # Set by the daemon via set_pass_ticker. The federation runner shares it.
        self._note_pass: Optional[Callable[[], None]] = None

        # Services (lazy loaded)
        self._splunk_service = None
        self._crowdstrike_service = None
        self._data_service = None
        self._azure_sentinel_service = None
        self._aws_security_hub_service = None
        self._microsoft_defender_service = None
        self._elastic_service = None
        self._opensearch_service = None
        # Resolved config each live client was built from, to spot edits.
        self._service_configs: Dict[str, Dict[str, Any]] = {}

        # Stats
        self.stats = {
            "splunk_polls": 0,
            "splunk_findings": 0,
            "crowdstrike_polls": 0,
            "crowdstrike_findings": 0,
            "azure_sentinel_polls": 0,
            "azure_sentinel_findings": 0,
            "aws_security_hub_polls": 0,
            "aws_security_hub_findings": 0,
            "microsoft_defender_polls": 0,
            "microsoft_defender_findings": 0,
            "elastic_polls": 0,
            "elastic_findings": 0,
            "opensearch_polls": 0,
            "opensearch_findings": 0,
            "webhook_findings": 0,
            "dropped": 0,
            "errors": 0,
            "webhook_rejections": {},
        }

    def set_loop_watcher(self, watch: Callable[..., None]) -> None:
        self._federation.watch_loop = watch

    def set_pass_ticker(self, tick: Callable[[], None]) -> None:
        """Count poll-loop passes and the webhook heartbeat on /health."""
        self._note_pass = tick
        self._federation.note_pass = tick

    def set_output_queue(self, queue: asyncio.Queue):
        """Set the output queue for processed findings."""
        self._output_queue = queue
        self._federation.set_output_queue(queue)

    @staticmethod
    def _build_splunk(cfg: Dict[str, Any]):
        from core.integrations.splunk.client import SplunkService

        return SplunkService(
            server_url=cfg["server_url"] or "",
            username=cfg["username"] or "",
            password=cfg["password"] or "",
            verify_ssl=cfg["verify_ssl"],
            ca_cert_path=cfg["ca_cert_path"],
        )

    @staticmethod
    def _build_crowdstrike(cfg: Dict[str, Any]):
        from core.integrations.crowdstrike.client import CrowdStrikeService

        return CrowdStrikeService(
            client_id=cfg["client_id"] or "",
            client_secret=cfg["client_secret"] or "",
            base_url=cfg["base_url"] or "https://api.crowdstrike.com",
        )

    @staticmethod
    def _sources() -> Dict[str, tuple]:
        """Attribute key -> (descriptor, client factory taking the resolved config).

        The ingestion clients resolve their own config in __init__, so their
        factories ignore the argument.
        """
        from core.integrations.aws_security_hub.descriptor import AWS_SECURITY_HUB
        from core.integrations.azure_sentinel.descriptor import AZURE_SENTINEL
        from core.integrations.crowdstrike.descriptor import CROWDSTRIKE
        from core.integrations.elastic.descriptor import ELASTIC
        from core.integrations.microsoft_defender.descriptor import (
            MICROSOFT_DEFENDER,
        )
        from core.integrations.opensearch.descriptor import OPENSEARCH
        from core.integrations.splunk.descriptor import SPLUNK

        def ingestion(module: str, cls: str):
            def build(_cfg):
                import importlib

                mod = importlib.import_module(f"core.integrations.{module}.ingestion")
                return getattr(mod, cls)()

            return build

        return {
            "splunk": (SPLUNK, DataPoller._build_splunk),
            "crowdstrike": (CROWDSTRIKE, DataPoller._build_crowdstrike),
            "azure_sentinel": (
                AZURE_SENTINEL,
                ingestion("azure_sentinel", "AzureSentinelIngestion"),
            ),
            "aws_security_hub": (
                AWS_SECURITY_HUB,
                ingestion("aws_security_hub", "AWSSecurityHubIngestion"),
            ),
            "microsoft_defender": (
                MICROSOFT_DEFENDER,
                ingestion("microsoft_defender", "MicrosoftDefenderIngestion"),
            ),
            "elastic": (ELASTIC, ingestion("elastic", "ElasticIngestion")),
            "opensearch": (OPENSEARCH, ingestion("opensearch", "OpenSearchIngestion")),
        }

    def _refresh_service(self, key: str):
        """Re-read a source's switch and settings; return its current client.

        Called before every poll so switching an integration off stops its loop
        on the next tick, and a changed destination, credential or TLS setting
        rebuilds the client, without a daemon restart. Blocking: run in a thread.
        """
        from core.config import is_integration_enabled
        from core.integrations._base.config import resolve

        descriptor, build = self._sources()[key]
        attr = f"_{key}_service"
        if not is_integration_enabled(descriptor.id):
            if getattr(self, attr) is not None:
                logger.info("%s switched off; polling stopped", descriptor.id)
            setattr(self, attr, None)
            self._service_configs.pop(key, None)
            return None
        cfg = resolve(descriptor)
        if getattr(self, attr) is None or self._service_configs.get(key) != cfg:
            # Drop the old client first so a failed rebuild never polls on
            # settings the operator has replaced.
            setattr(self, attr, None)
            self._service_configs.pop(key, None)
            setattr(self, attr, build(cfg))
            self._service_configs[key] = cfg
            logger.info("%s service (re)initialized", descriptor.id)
        return getattr(self, attr)

    async def _current_service(self, key: str):
        return await asyncio.to_thread(self._refresh_service, key)

    def _init_services(self):
        """Initialize the data service; source clients are built per tick."""
        try:
            from core.storage.database_data_service import DatabaseDataService

            self._data_service = DatabaseDataService()
        except Exception as e:
            logger.error(f"Error initializing services: {e}")

    async def run(self, shutdown_event: asyncio.Event):
        """Run the polling loop."""
        logger.info("Data poller starting...")
        self._init_services()

        # Create polling tasks
        tasks = []

        # Federation runner is always spawned. It self-gates on the global
        # federation.settings toggle and per-source rows; idle while disabled.
        tasks.append(asyncio.create_task(self._federation.run(shutdown_event)))

        # Every source loop runs; each re-checks its integration's switch per
        # tick and idles while it is off, so switching one on needs no restart.
        tasks.append(asyncio.create_task(self._poll_splunk_loop(shutdown_event)))
        tasks.append(asyncio.create_task(self._poll_crowdstrike_loop(shutdown_event)))
        for source in self._INGESTION_SOURCES:
            tasks.append(
                asyncio.create_task(self._poll_ingestion_loop(source, shutdown_event))
            )
        tasks.append(asyncio.create_task(self._poll_elastic_loop(shutdown_event)))

        if self.config.webhook_enabled:
            tasks.append(asyncio.create_task(self._run_webhook_server(shutdown_event)))

        if not tasks:
            logger.warning("No data sources configured for polling")
            # Just wait for shutdown
            await shutdown_event.wait()
            return

        # Wait for all tasks or shutdown
        try:
            await asyncio.gather(*tasks)
        except asyncio.CancelledError:
            logger.info("Polling tasks cancelled")

    async def _poll_splunk_loop(self, shutdown_event: asyncio.Event):
        """Poll Splunk for new alerts on interval."""
        logger.info(
            f"Splunk polling loop started (interval: {self.config.splunk_interval}s)"
        )

        while not shutdown_event.is_set():
            try:
                await self._poll_splunk()
                self._splunk_state.last_poll_time = utcnow()
            except Exception as e:
                logger.error(f"Splunk polling error: {e}")
                self.stats["errors"] += 1

            # Wait for interval or shutdown
            try:
                await asyncio.wait_for(
                    shutdown_event.wait(), timeout=self.config.splunk_interval
                )
                break  # Shutdown requested
            except asyncio.TimeoutError:
                pass  # Continue polling

    async def _poll_splunk(self):
        """Poll Splunk for new security alerts."""
        if self._federation.is_active_for("splunk"):
            return  # Federation owns this source while globally + per-source enabled
        splunk = await self._current_service("splunk")
        if not splunk:
            return

        self.stats["splunk_polls"] += 1
        logger.debug("Polling Splunk for new alerts...")

        lookback = self._splunk_state.lookback_minutes(self.config.splunk_interval)
        earliest_time = f"-{lookback}m"

        # Query for notable events / security alerts
        queries = [
            "index=notable | head 100",
            "index=security sourcetype=*:alert* | head 100",
            "`notable` | head 100",
        ]

        # search() raises on any error, so a query failed if it raised. An empty
        # result ran and found nothing; it falls through, since on non-ES installs
        # `index=notable` is empty by design and the fallbacks must be reached.
        # A failed query raises, so the loop counts an error and leaves
        # last_poll_time alone, else a fallback's results would hide the events
        # it missed. The last query is exempt: the `notable` macro is undefined
        # without Enterprise Security, so it fails there, and as a last resort
        # after empty queries it cannot discard anything.
        findings = []
        for i, query in enumerate(queries):
            try:
                # search() polls its job with time.sleep for up to ~110s,
                # which would otherwise freeze the whole daemon loop.
                results = (
                    await asyncio.to_thread(
                        splunk.search,
                        query=query,
                        earliest_time=earliest_time,
                        latest_time="now",
                        max_count=100,
                    )
                ).results
            except Exception as e:
                logger.warning("Splunk query failed (%s): %s", query, e)
                if i < len(queries) - 1:
                    raise RuntimeError(f"Splunk: query failed ({query}): {e}") from e
                continue
            if results:
                findings.extend(results)
                break  # Use first successful query

        # Process findings
        new_count = 0
        for event in findings:
            finding = _splunk_event_to_finding(event)
            if finding and not await self._splunk_dedup.is_processed(
                finding["finding_id"]
            ):
                if await self._enqueue_finding(
                    finding, "splunk", self._splunk_dedup, finding["finding_id"]
                ):
                    await self._splunk_dedup.mark_processed(finding["finding_id"])
                    new_count += 1

        if new_count > 0:
            logger.info(f"Polled {new_count} new findings from Splunk")
            self.stats["splunk_findings"] += new_count

    async def _poll_crowdstrike_loop(self, shutdown_event: asyncio.Event):
        """Poll CrowdStrike for new detections on interval."""
        logger.info(
            f"CrowdStrike polling loop started (interval: {self.config.crowdstrike_interval}s)"
        )

        while not shutdown_event.is_set():
            try:
                await self._poll_crowdstrike()
                self._crowdstrike_state.last_poll_time = utcnow()
            except Exception as e:
                logger.error(f"CrowdStrike polling error: {e}")
                self.stats["errors"] += 1

            try:
                await asyncio.wait_for(
                    shutdown_event.wait(), timeout=self.config.crowdstrike_interval
                )
                break
            except asyncio.TimeoutError:
                pass

    async def _poll_crowdstrike(self):
        """Poll CrowdStrike for new alerts."""
        if self._federation.is_active_for("crowdstrike"):
            return
        crowdstrike = await self._current_service("crowdstrike")
        if not crowdstrike:
            return

        self.stats["crowdstrike_polls"] += 1
        logger.debug("Polling CrowdStrike for new alerts...")

        try:
            lookback = self._crowdstrike_state.lookback_minutes(
                self.config.crowdstrike_interval
            )
            since = utcnow() - timedelta(minutes=lookback)

            alerts = await asyncio.to_thread(
                crowdstrike.get_alerts,
                filter_query=endpoint_alert_filter(since),
                limit=100,
            )

            # None means the query failed; raise so the loop counts an error
            # and leaves last_poll_time alone. [] is a clean empty poll.
            if alerts is None:
                detail = getattr(crowdstrike, "last_error", None)
                raise RuntimeError(
                    "CrowdStrike alerts query failed"
                    + (f": {detail}" if detail else "")
                )
            if not alerts:
                return

            new_count = 0
            for alert in alerts:
                finding = _alert_to_finding(alert)
                if finding and not await self._crowdstrike_dedup.is_processed(
                    finding["finding_id"]
                ):
                    if await self._enqueue_finding(
                        finding,
                        "crowdstrike",
                        self._crowdstrike_dedup,
                        finding["finding_id"],
                    ):
                        await self._crowdstrike_dedup.mark_processed(
                            finding["finding_id"]
                        )
                        new_count += 1

            if new_count > 0:
                logger.info(f"Polled {new_count} new alerts from CrowdStrike")
                self.stats["crowdstrike_findings"] += new_count

        except Exception as e:
            logger.error(f"CrowdStrike API error: {e}")
            raise

    async def _run_webhook_server(self, shutdown_event: asyncio.Event):
        """Run a simple webhook server for external ingestion."""
        from aiohttp import web

        def reject(request: web.Request, reason: str, detail: Optional[str] = None):
            record_rejection(
                f"daemon{request.path}", reason, request.remote, detail=detail
            )
            self.stats["webhook_rejections"] = rejection_counts("daemon/")

        async def handle_webhook(request: web.Request) -> web.Response:
            """Handle incoming webhook data."""
            # Fail closed: no token configured => ingestion is disabled, and every
            # request must present a matching bearer (constant-time compare).
            token = self.config.webhook_token
            if not token:
                reject(request, NO_SECRET, "DAEMON_WEBHOOK_TOKEN is not set")
                return web.json_response(
                    {"error": "ingest disabled: server missing DAEMON_WEBHOOK_TOKEN"},
                    status=503,
                )
            presented = (
                request.headers.get("Authorization", "").removeprefix("Bearer ").strip()
            )
            if not hmac.compare_digest(presented, token):
                reject(request, BAD_TOKEN)
                return web.json_response({"error": "unauthorized"}, status=401)
            try:
                data = await request.json()

                # Support batch or single finding
                findings = data if isinstance(data, list) else [data]

                # Block pushes for a disabled integration: 503 the whole batch so
                # the connector holds its cursor and retries (nothing dropped; feed
                # resumes on re-enable). Only registered-but-disabled sources are
                # blocked, so generic 'webhook'/'flow' pushes are unaffected.
                from core.storage.config_service import get_config_service

                # Run the sync SQLAlchemy lookup off the event loop so a slow or
                # locked DB can't freeze the whole daemon on the ingest hot path.
                disabled = await asyncio.to_thread(
                    lambda: get_config_service().get_disabled_integration_ids()
                )
                blocked = {
                    s for f in findings if (s := f.get("data_source")) in disabled
                }
                if blocked:
                    reject(
                        request,
                        DISABLED,
                        f"sources={sorted(blocked)[:10]}",
                    )
                    return web.json_response(
                        {
                            "error": f"ingestion disabled for source(s): {sorted(blocked)}"
                        },
                        status=503,
                    )

                for finding_data in findings:
                    finding_id = finding_data.get("finding_id")
                    if not finding_id:
                        import uuid

                        finding_id = f"webhook-{uuid.uuid4().hex[:16]}"
                        finding_data["finding_id"] = finding_id

                # Untrusted payload: coerce to {technique: score or null} once
                # here, before anything is enqueued, so a bad entry 400s the
                # whole batch (via the except below) instead of breaking triage.
                for finding_data in findings:
                    finding_data["mitre_predictions"] = normalize_mitre_predictions(
                        finding_data.get("mitre_predictions"),
                        finding_data["finding_id"],
                        data_source=finding_data.get("data_source"),
                    )

                count = 0
                for finding_data in findings:
                    finding_id = finding_data["finding_id"]
                    if not await self._webhook_dedup.is_processed(finding_id):
                        finding_data["data_source"] = finding_data.get(
                            "data_source", "webhook"
                        )
                        if await self._enqueue_finding(
                            finding_data,
                            "webhook",
                            self._webhook_dedup,
                            finding_id,
                        ):
                            await self._webhook_dedup.mark_processed(finding_id)
                            count += 1

                self.stats["webhook_findings"] += count
                labels = []
                for finding_data in findings:
                    source = finding_data.get("data_source")
                    labels.append(
                        source.strip()
                        if isinstance(source, str) and source.strip()
                        else "webhook"
                    )
                await asyncio.to_thread(
                    note_success,
                    DAEMON_SOURCE,
                    received=count,
                    labels=labels,
                )
                return web.json_response({"status": "ok", "ingested": count})

            except Exception as e:
                logger.error(f"Webhook error: {e}")
                await asyncio.to_thread(note_dropped, DAEMON_SOURCE)
                return web.json_response({"error": str(e)}, status=400)

        async def health_check(request: web.Request) -> web.Response:
            """Health check endpoint."""
            return web.json_response({"status": "healthy"})

        app = web.Application()
        app.router.add_post("/ingest", handle_webhook)
        app.router.add_post("/webhook", handle_webhook)
        app.router.add_get("/health", health_check)

        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(
            runner, get_settings().daemon_webhook_bind_host, self.config.webhook_port
        )

        logger.info(f"Webhook server starting on port {self.config.webhook_port}")
        try:
            await site.start()
        except OSError as e:
            # Don't re-raise: the other polling tasks must keep running.
            logger.error(
                "Webhook server failed to bind port %s: %s", self.config.webhook_port, e
            )
            await runner.cleanup()
            return

        # Heartbeat while this task is serving. A failed bind returned above
        # and did not tick. About a second, inside this task.
        while not shutdown_event.is_set():
            if self._note_pass is not None:
                self._note_pass()
            try:
                await asyncio.wait_for(shutdown_event.wait(), timeout=1.0)
                break
            except asyncio.TimeoutError:
                pass

        await runner.cleanup()
        logger.info("Webhook server stopped")

    async def _enqueue_finding(
        self,
        finding: Dict[str, Any],
        source: str,
        dedup: Optional[RedisDedupSet] = None,
        dedup_key: Optional[str] = None,
    ) -> bool:
        """Hand a finding off for processing. True if it was accepted.

        Callers must not mark a finding processed unless this returns True:
        the dedup key is what makes a retry possible, so marking a finding that
        was never stored drops it permanently.

        On the queue path the item carries the dedup set and the exact key
        about to be marked. If the processor gives up on the store, it forgets
        that key on this same instance. The queue is still in-process, so a
        daemon stop between put and the store drops the finding. A durable
        queue is a separate change.
        """
        if self._output_queue:
            await self._output_queue.put(
                {
                    "type": "finding",
                    "source": source,
                    "data": finding,
                    "timestamp": utcnow().isoformat(),
                    "dedup": dedup,
                    "dedup_key": dedup_key,
                }
            )
            logger.debug(f"Enqueued finding {finding.get('finding_id')} from {source}")
            return True

        if not self._data_service:
            logger.error(
                "Dropping finding %s from %s: no output queue and no data service",
                finding.get("finding_id"),
                source,
            )
            return False

        try:
            from core.ingestion.ingestion_service import IngestionService

            IngestionService().ingest_finding(finding)
            return True
        except Exception:
            logger.exception(
                "Failed to store finding %s from %s; leaving it unmarked to retry",
                finding.get("finding_id"),
                source,
            )
            return False

    # Sources that ingest through their own service's ingest_alerts(limit=...).
    # (attribute/stat/federation key -> display label, what it calls a record)
    _INGESTION_SOURCES = {
        "azure_sentinel": ("Azure Sentinel", "incidents"),
        "aws_security_hub": ("AWS Security Hub", "findings"),
        "microsoft_defender": ("Microsoft Defender", "alerts"),
        "opensearch": ("OpenSearch", "findings"),
    }

    async def _poll_ingestion_source(self, source: str):
        """Poll one ingestion-service source. Raises so the loop counts it."""
        label, noun = self._INGESTION_SOURCES[source]
        if self._federation.is_active_for(source):
            return
        service = await self._current_service(source)
        if not service:
            return

        self.stats[f"{source}_polls"] += 1
        logger.debug("Polling %s for new %s...", label, noun)

        # ingest_alerts() calls asyncio.run(), which raises RuntimeError while
        # this loop is running. A worker thread also keeps the blocking SDK
        # and ingest_finding writes off the daemon loop.
        result = await asyncio.to_thread(service.ingest_alerts, limit=100)

        if not result.get("success"):
            # Raise rather than log: the loop is what counts an error and what
            # decides whether to stamp last_poll_time. Returning quietly here
            # recorded a failed poll as a clean one -- the same blind spot the
            # bare `except: log` in these pollers used to have, reached by the
            # other branch.
            raise IngestionError(f"{label} ingestion failed: {result.get('errors')}")

        ingested = result.get("ingested", 0)
        failed = result.get("failed", 0)
        self.stats[f"{source}_findings"] += ingested
        # A partial failure is not an outage (no raise, no backoff) but is counted.
        self.stats["dropped"] += failed
        logger.info("%s: ingested %d %s", label, ingested, noun)
        if failed:
            logger.warning(
                "%s: %d %s failed to ingest (first errors: %s)",
                label,
                failed,
                noun,
                result.get("errors"),
            )

    async def _poll_ingestion_loop(self, source: str, shutdown_event: asyncio.Event):
        """Poll an ingestion-service source on interval until shutdown."""
        label, _ = self._INGESTION_SOURCES[source]
        state = getattr(self, f"_{source}_state")
        interval = self.config.splunk_interval  # Use same interval as Splunk
        logger.info(f"{label} polling loop started (interval: {interval}s)")

        while not shutdown_event.is_set():
            try:
                await self._poll_ingestion_source(source)
                state.last_poll_time = utcnow()
            except Exception as e:
                logger.error(f"{label} polling error: {e}")
                self.stats["errors"] += 1

            # Wait for interval or shutdown
            try:
                await asyncio.wait_for(shutdown_event.wait(), timeout=interval)
                break  # Shutdown requested
            except asyncio.TimeoutError:
                pass  # Continue polling

    async def _poll_elastic_loop(self, shutdown_event: asyncio.Event):
        """Poll Elastic Security for new detection alerts on interval."""
        interval = self.config.splunk_interval  # Use same interval as Splunk
        logger.info(f"Elastic Security polling loop started (interval: {interval}s)")

        while not shutdown_event.is_set():
            try:
                await self._poll_elastic()
                self._elastic_state.last_poll_time = utcnow()
            except Exception as e:
                logger.error(f"Elastic Security polling error: {e}")
                self.stats["errors"] += 1

            try:
                await asyncio.wait_for(shutdown_event.wait(), timeout=interval)
                break
            except asyncio.TimeoutError:
                pass

    async def _poll_elastic(self):
        """Poll Elastic Security for new detection alerts."""
        if self._federation.is_active_for("elastic"):
            return
        elastic = await self._current_service("elastic")
        if not elastic:
            return

        self.stats["elastic_polls"] += 1
        logger.debug("Polling Elastic Security for new alerts...")

        try:
            lookback = self._elastic_state.lookback_minutes(self.config.splunk_interval)
            start_time = utcnow() - timedelta(minutes=lookback)

            alerts = await elastic.fetch_alerts(start_time=start_time, limit=100)

            new_count = 0
            dropped = 0
            for alert in alerts:
                finding = elastic.transform_alert_to_finding(alert)
                if not finding:
                    dropped += 1
                    continue
                if not await self._elastic_dedup.is_processed(finding["finding_id"]):
                    if await self._enqueue_finding(
                        finding, "elastic", self._elastic_dedup, finding["finding_id"]
                    ):
                        await self._elastic_dedup.mark_processed(finding["finding_id"])
                        new_count += 1

            if dropped:
                logger.warning(
                    "Elastic Security: dropped %d of %d alert(s) that failed to transform",
                    dropped,
                    len(alerts),
                )
                self.stats["dropped"] += dropped

            if new_count > 0:
                logger.info(f"Polled {new_count} new findings from Elastic Security")
                self.stats["elastic_findings"] += new_count

        except Exception as e:
            logger.error(f"Elastic Security API error: {e}")
            raise
