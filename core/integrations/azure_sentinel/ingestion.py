"""
Azure Sentinel Ingestion Service - Ingest incidents from Azure Sentinel.

Fetches security incidents from Microsoft Sentinel (Azure Sentinel) and converts them to findings.
"""

import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from core.ingestion.siem_ingestion_service import SIEMIngestionService
from core.integrations._base.config import resolve
from core.integrations._base.ids import EXTERNAL_ID_MAX, FINDING_ID_MAX, fit_id
from core.integrations.azure_sentinel.descriptor import AZURE_SENTINEL
from core.time import utcnow

logger = logging.getLogger(__name__)


class AzureSentinelIngestion(SIEMIngestionService):
    """Azure Sentinel ingestion service."""

    def __init__(self):
        """Initialize Azure Sentinel ingestion."""
        super().__init__()
        self.siem_name = "Azure Sentinel"
        self.config = resolve(AZURE_SENTINEL)

    async def fetch_alerts(
        self,
        start_time: Optional[datetime] = None,
        end_time: Optional[datetime] = None,
        limit: int = 100,
        oldest_first: bool = False,
    ) -> List[Dict[str, Any]]:
        """
        Fetch incidents from Azure Sentinel.

        Args:
            start_time: Start time for incident query
            end_time: End time for incident query
            limit: Maximum number of incidents to fetch
            oldest_first: Return the ``limit`` oldest incidents in the window,
                sorted by creation time. The SDK iterator is unordered, so this
                scans the whole window before applying ``limit``. Federation
                asks for this; the default stops at ``limit`` in API order, as
                the daemon poller has always read.

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
                start_time = utcnow() - timedelta(hours=24)
            if not end_time:
                end_time = utcnow()

            # Fetch incidents
            incidents = []
            created_times = []  # parallel to incidents; the oldest_first sort key
            incident_list = client.incidents.list(
                resource_group_name=resource_group, workspace_name=workspace_name
            )

            for incident in incident_list:
                # Filter by time. The SDK returns aware datetimes and the window
                # is naive UTC; comparing the two raises TypeError.
                created = incident.created_time_utc
                if created:
                    if created.tzinfo is not None:
                        created = created.astimezone(timezone.utc).replace(tzinfo=None)
                    if created < start_time or created > end_time:
                        continue

                created_times.append(created)  # naive UTC, or None when unset
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
                            incident.last_modified_time_utc.isoformat()
                            if incident.last_modified_time_utc
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
                            incident.additional_data.alerts_count
                            if incident.additional_data
                            else 0
                        ),
                    }
                )

                if not oldest_first and len(incidents) >= limit:
                    break

            if oldest_first:
                # Finish the scan first: the iterator is unordered, so stopping
                # at limit before sorting would hand back an arbitrary subset.
                # An incident with no created time sorts last, so it cannot
                # take a slot ahead of a dated one or anchor the cursor.
                order = sorted(
                    range(len(incidents)),
                    key=lambda i: (
                        created_times[i] is None,
                        created_times[i] or datetime.min,
                    ),
                )
                incidents = [incidents[i] for i in order[:limit]]

            logger.info(f"Fetched {len(incidents)} incidents from Azure Sentinel")
            return incidents

        except ImportError as e:
            # Raise, not []: an empty poll would be recorded as a success.
            msg = "Azure SDK not installed. Install: pip install azure-mgmt-securityinsight azure-identity"
            logger.error(msg)
            raise RuntimeError(msg) from e
        except Exception as e:
            logger.error(f"Error fetching Azure Sentinel incidents: {e}")
            # Raise, not []: federation must record the failure and keep its cursor.
            raise

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
            source_id = str(alert.get("id") or uuid.uuid4().hex[:12])
            finding_id = fit_id("sentinel-", source_id, FINDING_ID_MAX)

            # Extract entities
            entities = self.extract_entities(alert.get("properties", {}))

            # Build finding
            finding = {
                "finding_id": finding_id,
                # Same value the federation backfill derives for ids that fit.
                "external_id": fit_id("", f"sentinel-{source_id}", EXTERNAL_ID_MAX),
                "title": alert.get("title", "Azure Sentinel Incident"),
                "description": alert.get("description", ""),
                "severity": self.normalize_severity(alert.get("severity")),
                "data_source": "azure_sentinel",
                "timestamp": alert.get("created_time", utcnow().isoformat()),
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
