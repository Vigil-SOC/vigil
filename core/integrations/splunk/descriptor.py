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
        # Required for splunk-selfhosted, which reads this form; the official
        # server reads SPLUNK_MCP_URL and is held dormant until it is set.
        fields=(
            IntegrationField("server_url", required=True),
            IntegrationField("username", required=True),
            IntegrationField("password", secret=True, required=True),
            # Unset verifies: skipping it sends the login password to whoever answers.
            IntegrationField("verify_ssl", value_type="bool", default=True),
            IntegrationField("ca_cert_path"),
            IntegrationField("lookback_hours", value_type="int"),
        ),
    )
)
