"""
Splunk Ingestion Service - Ingest alerts from Splunk Enterprise Security.

Fetches notable events from Splunk ES and converts them to findings.
"""

import logging
from typing import List, Dict, Any, Optional
from datetime import datetime, timedelta
import uuid

from core.ingestion.siem_ingestion_service import SIEMIngestionService
from core.integrations.splunk.client import SplunkService
from core.config import get_integration_config

logger = logging.getLogger(__name__)


class SplunkIngestion(SIEMIngestionService):
    """Splunk ingestion service."""
    
    def __init__(self):
        """Initialize Splunk ingestion."""
        super().__init__()
        self.siem_name = "Splunk"
        self.config = get_integration_config('splunk')
        self.splunk_service = None
    
    def _get_splunk_service(self) -> Optional[SplunkService]:
        """
        Get or create Splunk service instance.
        
        Returns:
            SplunkService instance or None
        """
        if self.splunk_service:
            return self.splunk_service
        
        try:
            server_url = self.config.get('server_url')
            username = self.config.get('username')
            password = self.config.get('password')
            
            if not all([server_url, username, password]):
                logger.error("Splunk configuration incomplete")
                return None
            
            self.splunk_service = SplunkService(
                server_url=server_url,
                username=username,
                password=password,
                verify_ssl=self.config.get('verify_ssl', False)
            )
            
            # Test authentication
            if not self.splunk_service.authenticate():
                logger.error("Splunk authentication failed")
                return None
            
            return self.splunk_service
        
        except Exception as e:
            logger.error(f"Error creating Splunk service: {e}")
            return None
    
    async def fetch_alerts(
        self,
        start_time: Optional[datetime] = None,
        end_time: Optional[datetime] = None,
        limit: int = 100
    ) -> List[Dict[str, Any]]:
        """
        Fetch notable events from Splunk ES.
        
        Args:
            start_time: Start time for alert query
            end_time: End time for alert query
            limit: Maximum number of alerts to fetch
        
        Returns:
            List of raw alert dictionaries
        """
        try:
            splunk = self._get_splunk_service()
            if not splunk:
                return []
            
            # Set time range
            if not start_time:
                start_time = datetime.utcnow() - timedelta(hours=24)
            if not end_time:
                end_time = datetime.utcnow()
            
            # Calculate relative time
            hours_ago = int((datetime.utcnow() - start_time).total_seconds() / 3600)
            earliest_time = f"-{hours_ago}h"
            
            # Search for notable events (Splunk ES)
            query = '''
            search `notable` 
            | head {limit}
            | table _time, rule_name, rule_title, rule_description, severity, urgency, 
                    src, dest, user, src_user, dest_user, signature, signature_id, 
                    category, subcategory, mitre_tactic, mitre_technique, 
                    event_id, _raw
            '''.format(limit=limit)
            
            results = splunk.search(
                query=query,
                earliest_time=earliest_time,
                latest_time="now",
                max_count=limit
            )
            
            if not results:
                # Fallback: search for any security events
                query = '''
                search index=* sourcetype=* (severity=high OR severity=critical OR priority=high) 
                | head {limit}
                | table _time, host, source, sourcetype, severity, message, src_ip, dest_ip, 
                        user, action, signature, _raw
                '''.format(limit=limit)
                
                results = splunk.search(
                    query=query,
                    earliest_time=earliest_time,
                    latest_time="now",
                    max_count=limit
                )
            
            logger.info(f"Fetched {len(results) if results else 0} events from Splunk")
            return results or []
        
        except Exception as e:
            logger.error(f"Error fetching Splunk alerts: {e}")
            return []
    
    def transform_alert_to_finding(self, alert: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """
        Transform Splunk notable event to finding format.
        
        Args:
            alert: Raw event from Splunk
        
        Returns:
            Finding dictionary
        """
        try:
            # Generate finding ID
            event_id = alert.get('event_id', alert.get('_cd', uuid.uuid4().hex[:12]))
            finding_id = f"splunk-{event_id}"
            
            # Extract title and description
            title = alert.get('rule_title') or alert.get('rule_name') or alert.get('signature') or 'Splunk Event'
            description = alert.get('rule_description') or alert.get('message') or alert.get('_raw', '')[:500]
            
            # Extract severity
            severity = alert.get('severity') or alert.get('urgency') or 'medium'
            
            # Extract entities
            entities = {
                "ip_addresses": [],
                "domains": [],
                "usernames": [],
                "hostnames": [],
                "file_hashes": [],
            }
            
            # IPs
            for field in ['src', 'src_ip', 'dest', 'dest_ip', 'clientip']:
                if alert.get(field):
                    entities["ip_addresses"].append(str(alert[field]))
            
            # Usernames
            for field in ['user', 'src_user', 'dest_user', 'username']:
                if alert.get(field):
                    entities["usernames"].append(str(alert[field]))
            
            # Hostnames
            if alert.get('host'):
                entities["hostnames"].append(str(alert['host']))
            
            # MITRE ATT&CK
            tactics = []
            techniques = []
            
            if alert.get('mitre_tactic'):
                tactics = [alert['mitre_tactic']] if isinstance(alert['mitre_tactic'], str) else alert['mitre_tactic']
            if alert.get('mitre_technique'):
                techniques = [alert['mitre_technique']] if isinstance(alert['mitre_technique'], str) else alert['mitre_technique']
            
            # Build finding
            finding = {
                "finding_id": finding_id,
                "title": title,
                "description": description,
                "severity": self.normalize_severity(severity),
                "data_source": "splunk",
                "timestamp": alert.get('_time', datetime.utcnow().isoformat()),
                "raw_data": alert,
                "metadata": {
                    "event_id": event_id,
                    "rule_name": alert.get('rule_name'),
                    "category": alert.get('category'),
                    "subcategory": alert.get('subcategory'),
                    "signature": alert.get('signature'),
                    "signature_id": alert.get('signature_id'),
                    "source": alert.get('source'),
                    "sourcetype": alert.get('sourcetype'),
                    "host": alert.get('host'),
                    "action": alert.get('action'),
                },
                "entities": entities,
                "mitre_attack": {
                    "tactics": tactics,
                    "techniques": techniques,
                },
            }
            
            return finding
        
        except Exception as e:
            logger.error(f"Error transforming Splunk event: {e}")
            return None

