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
        # Two servers: the official one and the self-hosted one Vigil ships.
        mcp_server_names=("splunk", "splunk-selfhosted"),
        fields=(
            IntegrationField("server_url"),
            IntegrationField("username"),
            IntegrationField("password", secret=True),
            IntegrationField("verify_ssl", value_type="bool"),
            IntegrationField("ca_cert_path"),
            IntegrationField("lookback_hours", value_type="int"),
        ),
    )
)
