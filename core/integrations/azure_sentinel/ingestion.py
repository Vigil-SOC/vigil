"""
Azure Sentinel Ingestion Service - Ingest incidents from Azure Sentinel.

Fetches security incidents from Microsoft Sentinel (Azure Sentinel) and converts them to findings.
"""

import asyncio
import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from core.ingestion.siem_ingestion_service import SIEMIngestionService
from core.integrations._base.config import resolve
from core.integrations._base.config_gap import (
    report_config_complete,
    report_config_gap,
)
from core.integrations._base.ids import EXTERNAL_ID_MAX, FINDING_ID_MAX, fit_id
from core.integrations.azure_sentinel.descriptor import AZURE_SENTINEL
from core.time import utcnow

logger = logging.getLogger(__name__)

_SDK_MISSING = "Azure SDK not installed. Install: pip install azure-mgmt-securityinsight azure-identity"

# Sentinel entity kind -> (entity_context key, attributes holding the value).
_ENTITY_FIELDS = {
    "Ip": ("src_ips", ("address",)),
    "Host": ("hostnames", ("host_name",)),
    "Account": ("usernames", ("account_name",)),
    "Url": ("domains", ("url",)),
    "FileHash": ("file_hashes", ("hash_value",)),
}


def _iso(value: Optional[datetime]) -> Optional[str]:
    return value.isoformat() if value else None


def _entity_context(entities: List[Any]) -> Dict[str, List[str]]:
    """The entity_context keys a Defender finding uses, so recall keys match."""
    context: Dict[str, List[str]] = {key: [] for key, _ in _ENTITY_FIELDS.values()}
    for entity in entities:
        key, attrs = _ENTITY_FIELDS.get(getattr(entity, "kind", ""), (None, ()))
        for attr in attrs:
            value = getattr(entity, attr, None)
            if key and value and str(value) not in context[key]:
                context[key].append(str(value))
    return context


class AzureSentinelIngestion(SIEMIngestionService):
    """Azure Sentinel ingestion service."""

    def __init__(self):
        """Initialize Azure Sentinel ingestion."""
        super().__init__()
        self.siem_name = "Azure Sentinel"
        self.config = resolve(AZURE_SENTINEL)

    def _client(self):
        """(client, resource group, workspace), or None when config is missing.
        Raises ImportError when the Azure SDK is not installed."""
        from azure.identity import ClientSecretCredential
        from azure.mgmt.securityinsight import SecurityInsights

        # Get config
        tenant_id = self.config.get("tenant_id")
        client_id = self.config.get("client_id")
        client_secret = self.config.get("client_secret")
        subscription_id = self.config.get("subscription_id")
        resource_group = self.config.get("resource_group")
        workspace_name = self.config.get("workspace_name")

        required = {
            "tenant_id": tenant_id,
            "client_id": client_id,
            "client_secret": client_secret,
            "subscription_id": subscription_id,
            "resource_group": resource_group,
            "workspace_name": workspace_name,
        }
        missing = [name for name, value in required.items() if not value]
        if missing:
            report_config_gap(logger, "Azure Sentinel", missing)
            return None
        report_config_complete(logger, "Azure Sentinel")

        # Authenticate
        credential = ClientSecretCredential(
            tenant_id=tenant_id, client_id=client_id, client_secret=client_secret
        )

        return (
            SecurityInsights(credential, subscription_id),
            resource_group,
            workspace_name,
        )

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
            setup = self._client()
            if setup is None:
                return []
            client, resource_group, workspace_name = setup

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
            logger.error(_SDK_MISSING)
            raise RuntimeError(_SDK_MISSING) from e
        except Exception as e:
            logger.error(f"Error fetching Azure Sentinel incidents: {e}")
            # Raise, not []: federation must record the failure and keep its cursor.
            raise

    async def fetch_closed_history(self, days: int) -> List[Dict[str, Any]]:
        """Closed incidents created in the last ``days``, with their entities, as
        ticket rows for ``write_import`` (#3981). Raises when the Azure SDK or
        the config is missing, or a call fails."""
        try:
            setup = self._client()
        except ImportError as e:
            raise RuntimeError(_SDK_MISSING) from e
        if setup is None:
            raise RuntimeError("Azure Sentinel credentials are not configured")
        client, resource_group, workspace_name = setup
        start = utcnow() - timedelta(days=days)

        def read() -> List[Dict[str, Any]]:
            rows = []
            for incident in client.incidents.list(
                resource_group_name=resource_group, workspace_name=workspace_name
            ):
                created = incident.created_time_utc
                if created and created.tzinfo is not None:
                    created = created.astimezone(timezone.utc).replace(tzinfo=None)
                if incident.status != "Closed" or not created or created < start:
                    continue
                entities = client.incidents.list_entities(
                    resource_group_name=resource_group,
                    workspace_name=workspace_name,
                    incident_id=incident.name,
                )
                owner = incident.owner
                rows.append(
                    {
                        "id": incident.name,
                        "title": incident.title,
                        # An SDK enum; str() of one reads "IncidentClassification.X".
                        "disposition": getattr(
                            incident.classification, "value", incident.classification
                        ),
                        "opened_at": _iso(incident.created_time_utc),
                        # Sentinel has no closed-time property; a closed
                        # incident's last modification is the closest one.
                        "closed_at": _iso(incident.last_modified_time_utc),
                        "closed_by": owner
                        and (
                            owner.email
                            or owner.assigned_to
                            or owner.user_principal_name
                        ),
                        "close_notes": incident.classification_comment,
                        "entity_context": _entity_context(
                            getattr(entities, "entities", None) or []
                        ),
                        "source_system": "azure_sentinel",
                    }
                )
            return rows

        rows = await asyncio.to_thread(read)
        logger.info(f"Read {len(rows)} closed incidents from Azure Sentinel")
        return rows

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
