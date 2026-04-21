"""
SIEM Ingestion Service - Base class for SIEM integrations.

Provides common functionality for ingesting findings from various SIEM platforms.
"""

import logging
from typing import List, Dict, Any, Optional
from datetime import datetime
from abc import ABC, abstractmethod

from services.ingestion_service import IngestionService

logger = logging.getLogger(__name__)


class SIEMIngestionService(ABC):
    """Base class for SIEM ingestion services."""
    
    def __init__(self):
        """Initialize the SIEM ingestion service."""
        self.ingestion_service = IngestionService()
        self.siem_name = "Generic SIEM"
    
    @abstractmethod
    async def fetch_alerts(
        self,
        start_time: Optional[datetime] = None,
        end_time: Optional[datetime] = None,
        limit: int = 100
    ) -> List[Dict[str, Any]]:
        """
        Fetch alerts from the SIEM.
        
        Args:
            start_time: Start time for alert query
            end_time: End time for alert query
            limit: Maximum number of alerts to fetch
        
        Returns:
            List of raw alert dictionaries
        """
        pass
    
    @abstractmethod
    def transform_alert_to_finding(self, alert: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """
        Transform a SIEM alert into a finding format.
        
        Args:
            alert: Raw alert from SIEM
        
        Returns:
            Finding dictionary or None if transformation fails
        """
        pass
    
    def ingest_alerts(
        self,
        start_time: Optional[datetime] = None,
        end_time: Optional[datetime] = None,
        limit: int = 100
    ) -> Dict[str, Any]:
        """
        Fetch and ingest alerts from SIEM.
        
        Args:
            start_time: Start time for alert query
            end_time: End time for alert query
            limit: Maximum number of alerts to fetch
        
        Returns:
            Ingestion statistics
        """
        import asyncio
        
        try:
            # Fetch alerts
            alerts = asyncio.run(self.fetch_alerts(start_time, end_time, limit))
            
            if not alerts:
                logger.info(f"No alerts fetched from {self.siem_name}")
                return {
                    "success": True,
                    "siem": self.siem_name,
                    "fetched": 0,
                    "ingested": 0,
                    "failed": 0,
                    "errors": []
                }
            
            logger.info(f"Fetched {len(alerts)} alerts from {self.siem_name}")
            
            # Transform and ingest
            ingested = 0
            failed = 0
            errors = []
            
            for alert in alerts:
                try:
                    finding = self.transform_alert_to_finding(alert)
                    if finding:
                        success = self.ingestion_service.ingest_finding(finding)
                        if success:
                            ingested += 1
                        else:
                            failed += 1
                            errors.append(f"Failed to ingest alert: {alert.get('id', 'unknown')}")
                    else:
                        failed += 1
                        errors.append(f"Failed to transform alert: {alert.get('id', 'unknown')}")
                except Exception as e:
                    failed += 1
                    errors.append(f"Error processing alert: {str(e)}")
                    logger.error(f"Error processing alert from {self.siem_name}: {e}")
            
            logger.info(f"{self.siem_name} ingestion: {ingested} ingested, {failed} failed")
            
            return {
                "success": True,
                "siem": self.siem_name,
                "fetched": len(alerts),
                "ingested": ingested,
                "failed": failed,
                "errors": errors[:10]  # Limit error messages
            }
        
        except Exception as e:
            logger.error(f"Error ingesting from {self.siem_name}: {e}")
            return {
                "success": False,
                "siem": self.siem_name,
                "fetched": 0,
                "ingested": 0,
                "failed": 0,
                "errors": [str(e)]
            }
    
    async def update_upstream_alert_status(
        self,
        alert_id: str,
        status: str,
        note: Optional[str] = None,
    ) -> bool:
        """Push a status change back to the upstream SIEM.

        Override in subclasses that support bi-directional sync.
        Returns True on success, False on failure or if not supported.
        """
        raise NotImplementedError(
            f"{self.siem_name} does not support upstream status sync"
        )

    def normalize_severity(self, severity: Any) -> str:
        """
        Normalize severity to standard values.
        
        Args:
            severity: Raw severity value
        
        Returns:
            Normalized severity: critical, high, medium, low, info
        """
        if not severity:
            return "medium"
        
        severity_str = str(severity).lower()
        
        # Map various severity formats
        if any(s in severity_str for s in ['critical', 'crit', '5', 'emergency']):
            return "critical"
        elif any(s in severity_str for s in ['high', '4', 'error']):
            return "high"
        elif any(s in severity_str for s in ['medium', 'med', '3', 'warning', 'warn']):
            return "medium"
        elif any(s in severity_str for s in ['low', '2', 'notice']):
            return "low"
        elif any(s in severity_str for s in ['info', 'informational', '1', 'debug']):
            return "info"
        else:
            return "medium"
    
    def extract_entities(self, alert: Dict[str, Any]) -> Dict[str, List[str]]:
        """
        Extract entities (IPs, domains, users, etc.) from alert.
        
        Args:
            alert: Raw alert data
        
        Returns:
            Dictionary of entity types and values
        """
        entities = {
            "ip_addresses": [],
            "domains": [],
            "usernames": [],
            "hostnames": [],
            "file_hashes": [],
        }
        
        # Common field names for entities
        ip_fields = ['src_ip', 'dst_ip', 'source_ip', 'dest_ip', 'ip', 'ip_address']
        domain_fields = ['domain', 'hostname', 'dest_domain', 'query']
        user_fields = ['user', 'username', 'user_name', 'account', 'src_user', 'dst_user']
        host_fields = ['host', 'hostname', 'computer_name', 'device_name']
        hash_fields = ['hash', 'file_hash', 'md5', 'sha1', 'sha256']
        
        # Extract IPs
        for field in ip_fields:
            if field in alert and alert[field]:
                entities["ip_addresses"].append(str(alert[field]))
        
        # Extract domains
        for field in domain_fields:
            if field in alert and alert[field]:
                entities["domains"].append(str(alert[field]))
        
        # Extract usernames
        for field in user_fields:
            if field in alert and alert[field]:
                entities["usernames"].append(str(alert[field]))
        
        # Extract hostnames
        for field in host_fields:
            if field in alert and alert[field]:
                entities["hostnames"].append(str(alert[field]))
        
        # Extract file hashes
        for field in hash_fields:
            if field in alert and alert[field]:
                entities["file_hashes"].append(str(alert[field]))
        
        # Deduplicate
        for key in entities:
            entities[key] = list(set(entities[key]))
        
        return entities

