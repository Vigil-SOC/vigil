"""Data source polling for the SOC daemon."""

import asyncio
import logging
from datetime import datetime, timedelta
from typing import Optional, Dict, List, Any, Set
from dataclasses import dataclass, field

from daemon.config import PollingConfig

logger = logging.getLogger(__name__)


@dataclass
class PollState:
    """Track polling state for deduplication."""
    last_poll_time: Optional[datetime] = None
    processed_ids: Set[str] = field(default_factory=set)
    max_processed_ids: int = 10000  # Limit memory usage
    
    def mark_processed(self, finding_id: str):
        """Mark a finding as processed."""
        self.processed_ids.add(finding_id)
        # Trim if too large (FIFO-ish)
        if len(self.processed_ids) > self.max_processed_ids:
            # Remove oldest half
            to_remove = list(self.processed_ids)[:self.max_processed_ids // 2]
            for item in to_remove:
                self.processed_ids.discard(item)
    
    def is_processed(self, finding_id: str) -> bool:
        """Check if a finding was already processed."""
        return finding_id in self.processed_ids


class DataPoller:
    """Polls various data sources for new security findings."""
    
    def __init__(self, config: PollingConfig):
        self.config = config
        self._output_queue: Optional[asyncio.Queue] = None
        
        # Polling state for each source
        self._splunk_state = PollState()
        self._crowdstrike_state = PollState()
        self._azure_sentinel_state = PollState()
        self._aws_security_hub_state = PollState()
        self._microsoft_defender_state = PollState()
        self._elastic_state = PollState()
        self._generic_state = PollState()

        # Services (lazy loaded)
        self._splunk_service = None
        self._crowdstrike_service = None
        self._data_service = None
        self._azure_sentinel_service = None
        self._aws_security_hub_service = None
        self._microsoft_defender_service = None
        self._elastic_service = None

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
            "webhook_findings": 0,
            "errors": 0
        }
    
    def set_output_queue(self, queue: asyncio.Queue):
        """Set the output queue for processed findings."""
        self._output_queue = queue
    
    def _init_services(self):
        """Initialize data source services."""
        try:
            from core.config import get_integration_config, is_integration_enabled
            
            # Initialize Splunk service if configured
            if is_integration_enabled('splunk'):
                try:
                    from services.splunk_service import SplunkService
                    splunk_config = get_integration_config('splunk')
                    self._splunk_service = SplunkService(
                        server_url=splunk_config.get('server_url', ''),
                        username=splunk_config.get('username', ''),
                        password=splunk_config.get('password', ''),
                        verify_ssl=splunk_config.get('verify_ssl', False)
                    )
                    logger.info("Splunk service initialized")
                except Exception as e:
                    logger.warning(f"Failed to initialize Splunk service: {e}")
            
            # Initialize CrowdStrike service if configured
            if is_integration_enabled('crowdstrike'):
                try:
                    from services.crowdstrike_service import CrowdStrikeService
                    cs_config = get_integration_config('crowdstrike')
                    self._crowdstrike_service = CrowdStrikeService(
                        client_id=cs_config.get('client_id', ''),
                        client_secret=cs_config.get('client_secret', ''),
                        base_url=cs_config.get('base_url', 'https://api.crowdstrike.com')
                    )
                    logger.info("CrowdStrike service initialized")
                except Exception as e:
                    logger.warning(f"Failed to initialize CrowdStrike service: {e}")
            
            # Initialize Azure Sentinel service if configured
            if is_integration_enabled('azure_sentinel'):
                try:
                    from services.azure_sentinel_ingestion import AzureSentinelIngestion
                    self._azure_sentinel_service = AzureSentinelIngestion()
                    logger.info("Azure Sentinel service initialized")
                except Exception as e:
                    logger.warning(f"Failed to initialize Azure Sentinel service: {e}")
            
            # Initialize AWS Security Hub service if configured
            if is_integration_enabled('aws_security_hub'):
                try:
                    from services.aws_security_hub_ingestion import AWSSecurityHubIngestion
                    self._aws_security_hub_service = AWSSecurityHubIngestion()
                    logger.info("AWS Security Hub service initialized")
                except Exception as e:
                    logger.warning(f"Failed to initialize AWS Security Hub service: {e}")
            
            # Initialize Microsoft Defender service if configured
            if is_integration_enabled('microsoft_defender'):
                try:
                    from services.microsoft_defender_ingestion import MicrosoftDefenderIngestion
                    self._microsoft_defender_service = MicrosoftDefenderIngestion()
                    logger.info("Microsoft Defender service initialized")
                except Exception as e:
                    logger.warning(f"Failed to initialize Microsoft Defender service: {e}")

            # Initialize Elastic Security service if configured
            if is_integration_enabled('elastic-siem'):
                try:
                    from services.elastic_ingestion import ElasticIngestion
                    self._elastic_service = ElasticIngestion()
                    logger.info("Elastic Security service initialized")
                except Exception as e:
                    logger.warning(f"Failed to initialize Elastic Security service: {e}")

            # Initialize data service for database access
            from services.database_data_service import DatabaseDataService
            self._data_service = DatabaseDataService()
            
        except Exception as e:
            logger.error(f"Error initializing services: {e}")
    
    async def run(self, shutdown_event: asyncio.Event):
        """Run the polling loop."""
        logger.info("Data poller starting...")
        self._init_services()
        
        # Create polling tasks
        tasks = []
        
        if self._splunk_service:
            tasks.append(asyncio.create_task(
                self._poll_splunk_loop(shutdown_event)
            ))
        
        if self._crowdstrike_service:
            tasks.append(asyncio.create_task(
                self._poll_crowdstrike_loop(shutdown_event)
            ))
        
        if self._azure_sentinel_service:
            tasks.append(asyncio.create_task(
                self._poll_azure_sentinel_loop(shutdown_event)
            ))
        
        if self._aws_security_hub_service:
            tasks.append(asyncio.create_task(
                self._poll_aws_security_hub_loop(shutdown_event)
            ))
        
        if self._microsoft_defender_service:
            tasks.append(asyncio.create_task(
                self._poll_microsoft_defender_loop(shutdown_event)
            ))

        if self._elastic_service:
            tasks.append(asyncio.create_task(
                self._poll_elastic_loop(shutdown_event)
            ))

        if self.config.webhook_enabled:
            tasks.append(asyncio.create_task(
                self._run_webhook_server(shutdown_event)
            ))
        
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
        logger.info(f"Splunk polling loop started (interval: {self.config.splunk_interval}s)")
        
        while not shutdown_event.is_set():
            try:
                await self._poll_splunk()
                self._splunk_state.last_poll_time = datetime.utcnow()
            except Exception as e:
                logger.error(f"Splunk polling error: {e}")
                self.stats["errors"] += 1
            
            # Wait for interval or shutdown
            try:
                await asyncio.wait_for(
                    shutdown_event.wait(),
                    timeout=self.config.splunk_interval
                )
                break  # Shutdown requested
            except asyncio.TimeoutError:
                pass  # Continue polling
    
    async def _poll_splunk(self):
        """Poll Splunk for new security alerts."""
        if not self._splunk_service:
            return
        
        self.stats["splunk_polls"] += 1
        logger.debug("Polling Splunk for new alerts...")
        
        # Calculate time range
        lookback_minutes = max(self.config.splunk_interval // 60 + 1, 5)
        earliest_time = f"-{lookback_minutes}m"
        
        # Query for notable events / security alerts
        queries = [
            'index=notable | head 100',
            'index=security sourcetype=*:alert* | head 100',
            '`notable` | head 100'
        ]
        
        findings = []
        for query in queries:
            try:
                results = self._splunk_service.search(
                    query=query,
                    earliest_time=earliest_time,
                    latest_time="now",
                    max_count=100
                )
                if results:
                    findings.extend(results)
                    break  # Use first successful query
            except Exception as e:
                logger.debug(f"Splunk query failed: {query} - {e}")
                continue
        
        # Process findings
        new_count = 0
        for event in findings:
            finding = self._splunk_event_to_finding(event)
            if finding and not self._splunk_state.is_processed(finding['finding_id']):
                await self._enqueue_finding(finding, "splunk")
                self._splunk_state.mark_processed(finding['finding_id'])
                new_count += 1
        
        if new_count > 0:
            logger.info(f"Polled {new_count} new findings from Splunk")
            self.stats["splunk_findings"] += new_count
    
    def _splunk_event_to_finding(self, event: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Convert Splunk event to finding format."""
        import uuid
        
        # Extract key fields
        event_id = event.get('_cd') or event.get('event_id') or str(uuid.uuid4())
        finding_id = f"splunk-{event_id[:32]}"
        
        # Determine severity
        severity_raw = event.get('urgency') or event.get('severity') or 'medium'
        severity_map = {
            'critical': 'critical', 'high': 'high', 'medium': 'medium',
            'low': 'low', 'info': 'low', 'informational': 'low'
        }
        severity = severity_map.get(severity_raw.lower(), 'medium')
        
        # Extract entity context
        entity_context = {
            'src_ips': [],
            'dest_ips': [],
            'hostnames': [],
            'usernames': []
        }
        
        for ip_field in ['src_ip', 'src', 'source_ip']:
            if event.get(ip_field):
                entity_context['src_ips'].append(event[ip_field])
        
        for ip_field in ['dest_ip', 'dest', 'destination_ip']:
            if event.get(ip_field):
                entity_context['dest_ips'].append(event[ip_field])
        
        for host_field in ['host', 'hostname', 'src_host', 'dest_host']:
            if event.get(host_field):
                entity_context['hostnames'].append(event[host_field])
        
        for user_field in ['user', 'username', 'src_user']:
            if event.get(user_field):
                entity_context['usernames'].append(event[user_field])
        
        return {
            'finding_id': finding_id,
            'data_source': 'splunk',
            'timestamp': event.get('_time') or datetime.utcnow().isoformat(),
            'severity': severity,
            'status': 'new',
            'title': event.get('search_name') or event.get('rule_name') or 'Splunk Alert',
            'description': event.get('description') or event.get('_raw', '')[:500],
            'entity_context': entity_context,
            'raw_event': event,
            'anomaly_score': 0.5,  # Default score
            'mitre_predictions': {},
            'embedding': []
        }
    
    async def _poll_crowdstrike_loop(self, shutdown_event: asyncio.Event):
        """Poll CrowdStrike for new detections on interval."""
        logger.info(f"CrowdStrike polling loop started (interval: {self.config.crowdstrike_interval}s)")
        
        while not shutdown_event.is_set():
            try:
                await self._poll_crowdstrike()
                self._crowdstrike_state.last_poll_time = datetime.utcnow()
            except Exception as e:
                logger.error(f"CrowdStrike polling error: {e}")
                self.stats["errors"] += 1
            
            try:
                await asyncio.wait_for(
                    shutdown_event.wait(),
                    timeout=self.config.crowdstrike_interval
                )
                break
            except asyncio.TimeoutError:
                pass
    
    async def _poll_crowdstrike(self):
        """Poll CrowdStrike for new detections."""
        if not self._crowdstrike_service:
            return
        
        self.stats["crowdstrike_polls"] += 1
        logger.debug("Polling CrowdStrike for new detections...")
        
        try:
            # Get recent detections
            lookback_minutes = max(self.config.crowdstrike_interval // 60 + 1, 5)
            since = datetime.utcnow() - timedelta(minutes=lookback_minutes)
            
            detections = self._crowdstrike_service.get_detections(
                filter_query=f"created_timestamp:>='{since.isoformat()}Z'",
                limit=100
            )
            
            if not detections:
                return
            
            new_count = 0
            for detection in detections:
                finding = self._crowdstrike_detection_to_finding(detection)
                if finding and not self._crowdstrike_state.is_processed(finding['finding_id']):
                    await self._enqueue_finding(finding, "crowdstrike")
                    self._crowdstrike_state.mark_processed(finding['finding_id'])
                    new_count += 1
            
            if new_count > 0:
                logger.info(f"Polled {new_count} new detections from CrowdStrike")
                self.stats["crowdstrike_findings"] += new_count
                
        except Exception as e:
            logger.error(f"CrowdStrike API error: {e}")
            raise
    
    def _crowdstrike_detection_to_finding(self, detection: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Convert CrowdStrike detection to finding format."""
        detection_id = detection.get('detection_id', '')
        if not detection_id:
            return None
        
        finding_id = f"cs-{detection_id[:32]}"
        
        # Map severity
        severity_raw = detection.get('max_severity_displayname', 'Medium')
        severity_map = {
            'Critical': 'critical', 'High': 'high', 'Medium': 'medium',
            'Low': 'low', 'Informational': 'low'
        }
        severity = severity_map.get(severity_raw, 'medium')
        
        # Extract behaviors and tactics
        behaviors = detection.get('behaviors', [])
        mitre_predictions = {}
        for behavior in behaviors:
            tactic = behavior.get('tactic')
            technique = behavior.get('technique')
            if technique:
                mitre_predictions[technique] = 0.9  # High confidence from EDR
        
        # Entity context
        device = detection.get('device', {})
        entity_context = {
            'src_ips': [device.get('local_ip')] if device.get('local_ip') else [],
            'hostnames': [device.get('hostname')] if device.get('hostname') else [],
            'usernames': [detection.get('user_name')] if detection.get('user_name') else [],
            'device_id': device.get('device_id')
        }
        
        return {
            'finding_id': finding_id,
            'data_source': 'crowdstrike',
            'timestamp': detection.get('created_timestamp') or datetime.utcnow().isoformat(),
            'severity': severity,
            'status': 'new',
            'title': detection.get('scenario') or 'CrowdStrike Detection',
            'description': detection.get('description', ''),
            'entity_context': entity_context,
            'raw_event': detection,
            'anomaly_score': detection.get('max_confidence', 50) / 100.0,
            'mitre_predictions': mitre_predictions,
            'embedding': []
        }
    
    async def _run_webhook_server(self, shutdown_event: asyncio.Event):
        """Run a simple webhook server for external ingestion."""
        from aiohttp import web
        
        async def handle_webhook(request: web.Request) -> web.Response:
            """Handle incoming webhook data."""
            try:
                data = await request.json()
                
                # Support batch or single finding
                findings = data if isinstance(data, list) else [data]
                
                count = 0
                for finding_data in findings:
                    finding_id = finding_data.get('finding_id')
                    if not finding_id:
                        import uuid
                        finding_id = f"webhook-{uuid.uuid4().hex[:16]}"
                        finding_data['finding_id'] = finding_id
                    
                    if not self._generic_state.is_processed(finding_id):
                        finding_data['data_source'] = finding_data.get('data_source', 'webhook')
                        await self._enqueue_finding(finding_data, "webhook")
                        self._generic_state.mark_processed(finding_id)
                        count += 1
                
                self.stats["webhook_findings"] += count
                return web.json_response({"status": "ok", "ingested": count})
            
            except Exception as e:
                logger.error(f"Webhook error: {e}")
                return web.json_response({"error": str(e)}, status=400)
        
        async def health_check(request: web.Request) -> web.Response:
            """Health check endpoint."""
            return web.json_response({"status": "healthy", "stats": self.stats})
        
        app = web.Application()
        app.router.add_post('/ingest', handle_webhook)
        app.router.add_post('/webhook', handle_webhook)
        app.router.add_get('/health', health_check)
        
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, '0.0.0.0', self.config.webhook_port)
        
        logger.info(f"Webhook server starting on port {self.config.webhook_port}")
        await site.start()
        
        # Wait for shutdown
        await shutdown_event.wait()
        
        await runner.cleanup()
        logger.info("Webhook server stopped")
    
    async def _enqueue_finding(self, finding: Dict[str, Any], source: str):
        """Add finding to output queue for processing."""
        if self._output_queue:
            await self._output_queue.put({
                "type": "finding",
                "source": source,
                "data": finding,
                "timestamp": datetime.utcnow().isoformat()
            })
            logger.debug(f"Enqueued finding {finding.get('finding_id')} from {source}")
        else:
            # No queue, store directly
            if self._data_service:
                try:
                    from services.ingestion_service import IngestionService
                    ingestion = IngestionService()
                    ingestion.ingest_finding(finding)
                except Exception as e:
                    logger.error(f"Failed to store finding: {e}")
    
    async def _poll_azure_sentinel_loop(self, shutdown_event: asyncio.Event):
        """Poll Azure Sentinel for new incidents on interval."""
        interval = self.config.splunk_interval  # Use same interval as Splunk
        logger.info(f"Azure Sentinel polling loop started (interval: {interval}s)")
        
        while not shutdown_event.is_set():
            try:
                await self._poll_azure_sentinel()
                self._azure_sentinel_state.last_poll_time = datetime.utcnow()
            except Exception as e:
                logger.error(f"Azure Sentinel polling error: {e}")
                self.stats["errors"] += 1
            
            # Wait for interval or shutdown
            try:
                await asyncio.wait_for(shutdown_event.wait(), timeout=interval)
                break  # Shutdown requested
            except asyncio.TimeoutError:
                pass  # Continue polling
    
    async def _poll_azure_sentinel(self):
        """Poll Azure Sentinel for new incidents."""
        if not self._azure_sentinel_service:
            return
        
        self.stats["azure_sentinel_polls"] += 1
        logger.debug("Polling Azure Sentinel for new incidents...")
        
        try:
            # Use ingestion service
            result = self._azure_sentinel_service.ingest_alerts(limit=100)
            
            if result.get("success"):
                ingested = result.get("ingested", 0)
                self.stats["azure_sentinel_findings"] += ingested
                logger.info(f"Azure Sentinel: ingested {ingested} incidents")
            else:
                logger.error(f"Azure Sentinel ingestion failed: {result.get('errors')}")
        except Exception as e:
            logger.error(f"Error polling Azure Sentinel: {e}")
    
    async def _poll_aws_security_hub_loop(self, shutdown_event: asyncio.Event):
        """Poll AWS Security Hub for new findings on interval."""
        interval = self.config.splunk_interval  # Use same interval as Splunk
        logger.info(f"AWS Security Hub polling loop started (interval: {interval}s)")
        
        while not shutdown_event.is_set():
            try:
                await self._poll_aws_security_hub()
                self._aws_security_hub_state.last_poll_time = datetime.utcnow()
            except Exception as e:
                logger.error(f"AWS Security Hub polling error: {e}")
                self.stats["errors"] += 1
            
            # Wait for interval or shutdown
            try:
                await asyncio.wait_for(shutdown_event.wait(), timeout=interval)
                break  # Shutdown requested
            except asyncio.TimeoutError:
                pass  # Continue polling
    
    async def _poll_aws_security_hub(self):
        """Poll AWS Security Hub for new findings."""
        if not self._aws_security_hub_service:
            return
        
        self.stats["aws_security_hub_polls"] += 1
        logger.debug("Polling AWS Security Hub for new findings...")
        
        try:
            # Use ingestion service
            result = self._aws_security_hub_service.ingest_alerts(limit=100)
            
            if result.get("success"):
                ingested = result.get("ingested", 0)
                self.stats["aws_security_hub_findings"] += ingested
                logger.info(f"AWS Security Hub: ingested {ingested} findings")
            else:
                logger.error(f"AWS Security Hub ingestion failed: {result.get('errors')}")
        except Exception as e:
            logger.error(f"Error polling AWS Security Hub: {e}")
    
    async def _poll_microsoft_defender_loop(self, shutdown_event: asyncio.Event):
        """Poll Microsoft Defender for new alerts on interval."""
        interval = self.config.splunk_interval  # Use same interval as Splunk
        logger.info(f"Microsoft Defender polling loop started (interval: {interval}s)")
        
        while not shutdown_event.is_set():
            try:
                await self._poll_microsoft_defender()
                self._microsoft_defender_state.last_poll_time = datetime.utcnow()
            except Exception as e:
                logger.error(f"Microsoft Defender polling error: {e}")
                self.stats["errors"] += 1
            
            # Wait for interval or shutdown
            try:
                await asyncio.wait_for(shutdown_event.wait(), timeout=interval)
                break  # Shutdown requested
            except asyncio.TimeoutError:
                pass  # Continue polling
    
    async def _poll_microsoft_defender(self):
        """Poll Microsoft Defender for new alerts."""
        if not self._microsoft_defender_service:
            return
        
        self.stats["microsoft_defender_polls"] += 1
        logger.debug("Polling Microsoft Defender for new alerts...")
        
        try:
            # Use ingestion service
            result = self._microsoft_defender_service.ingest_alerts(limit=100)
            
            if result.get("success"):
                ingested = result.get("ingested", 0)
                self.stats["microsoft_defender_findings"] += ingested
                logger.info(f"Microsoft Defender: ingested {ingested} alerts")
            else:
                logger.error(f"Microsoft Defender ingestion failed: {result.get('errors')}")
        except Exception as e:
            logger.error(f"Error polling Microsoft Defender: {e}")

    async def _poll_elastic_loop(self, shutdown_event: asyncio.Event):
        """Poll Elastic Security for new detection alerts on interval."""
        interval = self.config.splunk_interval  # Use same interval as Splunk
        logger.info(f"Elastic Security polling loop started (interval: {interval}s)")

        while not shutdown_event.is_set():
            try:
                await self._poll_elastic()
                self._elastic_state.last_poll_time = datetime.utcnow()
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
        if not self._elastic_service:
            return

        self.stats["elastic_polls"] += 1
        logger.debug("Polling Elastic Security for new alerts...")

        try:
            lookback_minutes = max(self.config.splunk_interval // 60 + 1, 5)
            start_time = datetime.utcnow() - timedelta(minutes=lookback_minutes)

            alerts = await self._elastic_service.fetch_alerts(
                start_time=start_time, limit=100
            )

            new_count = 0
            for alert in alerts:
                finding = self._elastic_service.transform_alert_to_finding(alert)
                if finding and not self._elastic_state.is_processed(finding["finding_id"]):
                    await self._enqueue_finding(finding, "elastic")
                    self._elastic_state.mark_processed(finding["finding_id"])
                    new_count += 1

            if new_count > 0:
                logger.info(f"Polled {new_count} new findings from Elastic Security")
                self.stats["elastic_findings"] += new_count

        except Exception as e:
            logger.error(f"Elastic Security API error: {e}")
            raise
