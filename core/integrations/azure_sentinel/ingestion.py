"""
Azure Sentinel Ingestion Service - Ingest incidents from Azure Sentinel.

Fetches security incidents from Microsoft Sentinel (Azure Sentinel) and converts them to findings.
"""

import logging
import uuid
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from core.config import get_integration_config
from core.ingestion.siem_ingestion_service import SIEMIngestionService

logger = logging.getLogger(__name__)


class AzureSentinelIngestion(SIEMIngestionService):
    """Azure Sentinel ingestion service."""

    def __init__(self):
        """Initialize Azure Sentinel ingestion."""
        super().__init__()
        self.siem_name = "Azure Sentinel"
        self.config = get_integration_config("azure-sentinel")

    async def fetch_alerts(
        self,
        start_time: Optional[datetime] = None,
        end_time: Optional[datetime] = None,
        limit: int = 100,
    ) -> List[Dict[str, Any]]:
        """
        Fetch incidents from Azure Sentinel.

        Args:
            start_time: Start time for incident query
            end_time: End time for incident query
            limit: Maximum number of incidents to fetch

        Returns:
            List of raw incident dictionaries
        """
        try:
            from azure.identity import ClientSecretCredential
            from azure.mgmt.securityinsight import SecurityInsights

            # Get config
            tenant_id = self.config.get("tenant_id")
            client_id = self.config.get("client_id")
            client_secret = self.config.get("client_secret")
            subscription_id = self.config.get("subscription_id")
            resource_group = self.config.get("resource_group")
            workspace_name = self.config.get("workspace_name")

            if not all(
                [
                    tenant_id,
                    client_id,
                    client_secret,
                    subscription_id,
                    resource_group,
                    workspace_name,
                ]
            ):
                logger.error("Azure Sentinel configuration incomplete")
                return []

            # Authenticate
            credential = ClientSecretCredential(
                tenant_id=tenant_id, client_id=client_id, client_secret=client_secret
            )

            # Create client
            client = SecurityInsights(credential, subscription_id)

            # Set time range
            if not start_time:
                start_time = datetime.utcnow() - timedelta(hours=24)
            if not end_time:
                end_time = datetime.utcnow()

            # Fetch incidents
            incidents = []
            incident_list = client.incidents.list(
                resource_group_name=resource_group, workspace_name=workspace_name
            )

            for incident in incident_list:
                # Filter by time
                if incident.created_time_utc:
                    if (
                        incident.created_time_utc < start_time
                        or incident.created_time_utc > end_time
                    ):
                        continue

                incidents.append(
                    {
                        "id": incident.name,
                        "title": incident.title,
                        "description": incident.description,
                        "severity": incident.severity,
                        "status": incident.status,
                        "created_time": (
                            incident.created_time_utc.isoformat()
                            if incident.created_time_utc
                            else None
                        ),
                        "last_updated_time": (
                            incident.last_updated_time_utc.isoformat()
                            if incident.last_updated_time_utc
                            else None
                        ),
                        "owner": incident.owner.email if incident.owner else None,
                        "labels": (
                            [label.label_name for label in incident.labels]
                            if incident.labels
                            else []
                        ),
                        "tactics": (
                            incident.additional_data.tactics
                            if incident.additional_data
                            else []
                        ),
                        "alert_count": (
                            incident.additional_data.alert_count
                            if incident.additional_data
                            else 0
                        ),
                        "properties": incident.additional_properties or {},
                    }
                )

                if len(incidents) >= limit:
                    break

            logger.info(f"Fetched {len(incidents)} incidents from Azure Sentinel")
            return incidents

        except ImportError:
            logger.error(
                "Azure SDK not installed. Install: pip install azure-mgmt-securityinsight azure-identity"
            )
            return []
        except Exception as e:
            logger.error(f"Error fetching Azure Sentinel incidents: {e}")
            return []

    def transform_alert_to_finding(
        self, alert: Dict[str, Any]
    ) -> Optional[Dict[str, Any]]:
        """
        Transform Azure Sentinel incident to finding format.

        Args:
            alert: Raw incident from Azure Sentinel

        Returns:
            Finding dictionary
        """
        try:
            # Generate finding ID
            finding_id = f"sentinel-{alert.get('id', uuid.uuid4().hex[:12])}"

            # Extract entities
            entities = self.extract_entities(alert.get("properties", {}))

            # Build finding
            finding = {
                "finding_id": finding_id,
                "title": alert.get("title", "Azure Sentinel Incident"),
                "description": alert.get("description", ""),
                "severity": self.normalize_severity(alert.get("severity")),
                "data_source": "azure_sentinel",
                "timestamp": alert.get("created_time", datetime.utcnow().isoformat()),
                "raw_data": alert,
                "metadata": {
                    "incident_id": alert.get("id"),
                    "status": alert.get("status"),
                    "owner": alert.get("owner"),
                    "labels": alert.get("labels", []),
                    "tactics": alert.get("tactics", []),
                    "alert_count": alert.get("alert_count", 0),
                    "last_updated": alert.get("last_updated_time"),
                },
                "entities": entities,
                "mitre_attack": {
                    "tactics": alert.get("tactics", []),
                    "techniques": [],
                },
            }

            return finding

        except Exception as e:
            logger.error(f"Error transforming Azure Sentinel incident: {e}")
            return None
