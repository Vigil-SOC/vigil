"""
AWS Security Hub Ingestion Service - Ingest findings from AWS Security Hub.

Fetches security findings from AWS Security Hub and converts them to the standard finding format.
"""

import logging
import uuid
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from core.config import get_integration_config
from core.ingestion.siem_ingestion_service import SIEMIngestionService

logger = logging.getLogger(__name__)


class AWSSecurityHubIngestion(SIEMIngestionService):
    """AWS Security Hub ingestion service."""

    def __init__(self):
        """Initialize AWS Security Hub ingestion."""
        super().__init__()
        self.siem_name = "AWS Security Hub"
        self.config = get_integration_config("aws-security-hub")

    async def fetch_alerts(
        self,
        start_time: Optional[datetime] = None,
        end_time: Optional[datetime] = None,
        limit: int = 100,
    ) -> List[Dict[str, Any]]:
        """
        Fetch findings from AWS Security Hub.

        Args:
            start_time: Start time for finding query
            end_time: End time for finding query
            limit: Maximum number of findings to fetch

        Returns:
            List of raw finding dictionaries
        """
        try:
            import boto3
            from botocore.exceptions import ClientError

            # Get config
            region = self.config.get("region", "us-east-1")
            access_key = self.config.get("access_key_id")
            secret_key = self.config.get("secret_access_key")

            # Create client
            if access_key and secret_key:
                client = boto3.client(
                    "securityhub",
                    region_name=region,
                    aws_access_key_id=access_key,
                    aws_secret_access_key=secret_key,
                )
            else:
                # Use default credentials
                client = boto3.client("securityhub", region_name=region)

            # Set time range
            if not start_time:
                start_time = datetime.utcnow() - timedelta(hours=24)
            if not end_time:
                end_time = datetime.utcnow()

            # Build filters
            filters = {
                "RecordState": [{"Value": "ACTIVE", "Comparison": "EQUALS"}],
                "WorkflowStatus": [{"Value": "NEW", "Comparison": "EQUALS"}],
                "CreatedAt": [
                    {
                        "Start": start_time.strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
                        "End": end_time.strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
                    }
                ],
            }

            # Fetch findings
            findings = []
            paginator = client.get_paginator("get_findings")

            for page in paginator.paginate(Filters=filters, MaxResults=min(limit, 100)):
                findings.extend(page["Findings"])
                if len(findings) >= limit:
                    break

            findings = findings[:limit]

            logger.info(f"Fetched {len(findings)} findings from AWS Security Hub")
            return findings

        except ImportError:
            logger.error("boto3 not installed. Install: pip install boto3")
            return []
        except ClientError as e:
            logger.error(f"AWS Security Hub API error: {e}")
            return []
        except Exception as e:
            logger.error(f"Error fetching AWS Security Hub findings: {e}")
            return []

    def transform_alert_to_finding(
        self, alert: Dict[str, Any]
    ) -> Optional[Dict[str, Any]]:
        """
        Transform AWS Security Hub finding to standard finding format.

        Args:
            alert: Raw finding from AWS Security Hub

        Returns:
            Finding dictionary
        """
        try:
            # Extract finding ID
            finding_id = f"aws-sh-{alert.get('Id', uuid.uuid4().hex[:12])}"

            # Extract severity
            severity_label = alert.get("Severity", {}).get("Label", "MEDIUM")
            severity = self.normalize_severity(severity_label)

            # Extract resources
            resources = alert.get("Resources", [])
            resource_ids = [r.get("Id", "") for r in resources]

            # Extract network info
            network = alert.get("Network", {})
            entities = {
                "ip_addresses": [],
                "domains": [],
                "usernames": [],
                "hostnames": resource_ids,
                "file_hashes": [],
            }

            if network.get("SourceIpV4"):
                entities["ip_addresses"].append(network["SourceIpV4"])
            if network.get("DestinationIpV4"):
                entities["ip_addresses"].append(network["DestinationIpV4"])
            if network.get("SourceDomain"):
                entities["domains"].append(network["SourceDomain"])
            if network.get("DestinationDomain"):
                entities["domains"].append(network["DestinationDomain"])

            # Extract MITRE ATT&CK info
            threat_intel = alert.get("ThreatIntelIndicators", [])
            tactics = []
            techniques = []

            for indicator in threat_intel:
                if "Category" in indicator:
                    tactics.append(indicator["Category"])

            # Build finding
            finding = {
                "finding_id": finding_id,
                "title": alert.get("Title", "AWS Security Hub Finding"),
                "description": alert.get("Description", ""),
                "severity": severity,
                "data_source": "aws_security_hub",
                "timestamp": alert.get("CreatedAt", datetime.utcnow().isoformat()),
                "raw_data": alert,
                "metadata": {
                    "aws_account_id": alert.get("AwsAccountId"),
                    "generator_id": alert.get("GeneratorId"),
                    "product_arn": alert.get("ProductArn"),
                    "product_name": alert.get("ProductName"),
                    "company_name": alert.get("CompanyName"),
                    "region": alert.get("Region"),
                    "resources": resource_ids,
                    "compliance": alert.get("Compliance", {}),
                    "workflow_state": alert.get("WorkflowState"),
                    "record_state": alert.get("RecordState"),
                },
                "entities": entities,
                "mitre_attack": {
                    "tactics": tactics,
                    "techniques": techniques,
                },
            }

            return finding

        except Exception as e:
            logger.error(f"Error transforming AWS Security Hub finding: {e}")
            return None
