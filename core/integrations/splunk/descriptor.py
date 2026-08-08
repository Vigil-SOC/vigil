"""Splunk integration descriptor — source of truth for Splunk's registry entries."""

from core.integrations._base.descriptor import (
    IntegrationDescriptor,
    IntegrationField,
    register_descriptor,
)

SPLUNK = register_descriptor(
    IntegrationDescriptor(
        id="splunk",
        category="SIEM",
        mcp_server_name="splunk-server",
        fields=(
            IntegrationField("server_url"),
            IntegrationField("username"),
            IntegrationField("password", secret=True),
            IntegrationField("verify_ssl"),
            IntegrationField("lookback_hours"),
        ),
    )
)
