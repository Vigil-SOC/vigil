"""
Microsoft Defender Ingestion Service - Ingest alerts from Microsoft Defender for Endpoint.

Fetches security alerts from Microsoft Defender and converts them to findings.
"""

import logging
from typing import List, Dict, Any, Optional
from datetime import datetime, timedelta
import uuid
import requests

from core.ingestion.siem_ingestion_service import SIEMIngestionService
from core.config import get_integration_config

logger = logging.getLogger(__name__)


class MicrosoftDefenderIngestion(SIEMIngestionService):
    """Microsoft Defender ingestion service."""
    
    def __init__(self):
        """Initialize Microsoft Defender ingestion."""
        super().__init__()
        self.siem_name = "Microsoft Defender"
        self.config = get_integration_config('microsoft-defender')
        self.access_token = None
    
    def _get_access_token(self) -> Optional[str]:
        """
        Get OAuth2 access token for Microsoft Defender API.
        
        Returns:
            Access token or None
        """
        try:
            tenant_id = self.config.get('tenant_id')
            client_id = self.config.get('client_id')
            client_secret = self.config.get('client_secret')
            
            if not all([tenant_id, client_id, client_secret]):
                logger.error("Microsoft Defender configuration incomplete")
                return None
            
            # Get token
            token_url = f"https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token"
            token_data = {
                'client_id': client_id,
                'client_secret': client_secret,
                'scope': 'https://api.securitycenter.microsoft.com/.default',
                'grant_type': 'client_credentials'
            }
            
            response = requests.post(token_url, data=token_data)
            response.raise_for_status()
            
            self.access_token = response.json()['access_token']
            return self.access_token
        
        except Exception as e:
            logger.error(f"Error getting Microsoft Defender access token: {e}")
            return None
    
    async def fetch_alerts(
        self,
        start_time: Optional[datetime] = None,
        end_time: Optional[datetime] = None,
        limit: int = 100
    ) -> List[Dict[str, Any]]:
        """
        Fetch alerts from Microsoft Defender.
        
        Args:
            start_time: Start time for alert query
            end_time: End time for alert query
            limit: Maximum number of alerts to fetch
        
        Returns:
            List of raw alert dictionaries
        """
        try:
            # Get access token
            token = self._get_access_token()
            if not token:
                return []
            
            # Set time range
            if not start_time:
                start_time = datetime.utcnow() - timedelta(hours=24)
            if not end_time:
                end_time = datetime.utcnow()
            
            # Build API request
            api_url = "https://api.securitycenter.microsoft.com/api/alerts"
            headers = {
                'Authorization': f'Bearer {token}',
                'Content-Type': 'application/json'
            }
            
            # Filter by time
            params = {
                '$filter': f"alertCreationTime ge {start_time.isoformat()}Z and alertCreationTime le {end_time.isoformat()}Z",
                '$top': limit,
                '$orderby': 'alertCreationTime desc'
            }
            
            response = requests.get(api_url, headers=headers, params=params)
            response.raise_for_status()
            
            alerts = response.json().get('value', [])
            
            logger.info(f"Fetched {len(alerts)} alerts from Microsoft Defender")
            return alerts
        
        except requests.exceptions.RequestException as e:
            logger.error(f"Microsoft Defender API error: {e}")
            return []
        except Exception as e:
            logger.error(f"Error fetching Microsoft Defender alerts: {e}")
            return []
    
    def transform_alert_to_finding(self, alert: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """
        Transform Microsoft Defender alert to finding format.
        
        Args:
            alert: Raw alert from Microsoft Defender
        
        Returns:
            Finding dictionary
        """
        try:
            # Generate finding ID
            finding_id = f"defender-{alert.get('id', uuid.uuid4().hex[:12])}"
            
            # Extract entities
            entities = {
                "ip_addresses": [],
                "domains": [],
                "usernames": [],
                "hostnames": [],
                "file_hashes": [],
            }
            
            # Extract from evidence
            evidence = alert.get('evidence', [])
            for item in evidence:
                entity_type = item.get('entityType', '').lower()
                
                if entity_type == 'ip':
                    entities["ip_addresses"].append(item.get('ipAddress', ''))
                elif entity_type == 'url':
                    entities["domains"].append(item.get('url', ''))
                elif entity_type == 'user':
                    entities["usernames"].append(item.get('userPrincipalName', ''))
                elif entity_type == 'machine':
                    entities["hostnames"].append(item.get('deviceDnsName', ''))
                elif entity_type == 'file':
                    if item.get('sha256'):
                        entities["file_hashes"].append(item['sha256'])
                    if item.get('sha1'):
                        entities["file_hashes"].append(item['sha1'])
                    if item.get('md5'):
                        entities["file_hashes"].append(item['md5'])
            
            # Extract MITRE ATT&CK
            mitre_techniques = alert.get('mitreTechniques', [])
            
            # Build finding
            finding = {
                "finding_id": finding_id,
                "title": alert.get('title', 'Microsoft Defender Alert'),
                "description": alert.get('description', ''),
                "severity": self.normalize_severity(alert.get('severity')),
                "data_source": "microsoft_defender",
                "timestamp": alert.get('alertCreationTime', datetime.utcnow().isoformat()),
                "raw_data": alert,
                "metadata": {
                    "alert_id": alert.get('id'),
                    "category": alert.get('category'),
                    "status": alert.get('status'),
                    "classification": alert.get('classification'),
                    "determination": alert.get('determination'),
                    "assigned_to": alert.get('assignedTo'),
                    "machine_id": alert.get('machineId'),
                    "detection_source": alert.get('detectionSource'),
                    "threat_family_name": alert.get('threatFamilyName'),
                    "first_activity": alert.get('firstActivityTime'),
                    "last_activity": alert.get('lastActivityTime'),
                },
                "entities": entities,
                "mitre_attack": {
                    "tactics": [],
                    "techniques": mitre_techniques,
                },
            }
            
            return finding
        
        except Exception as e:
            logger.error(f"Error transforming Microsoft Defender alert: {e}")
            return None

