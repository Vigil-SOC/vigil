"""Wazuh integration descriptor — source of truth for its registry entries.

The manager's server API only. Alerts come off the Wazuh indexer through the
Elastic or OpenSearch integration, not this one.
"""

from core.integrations._base.descriptor import (
    IntegrationDescriptor,
    IntegrationField,
    register_descriptor,
)

WAZUH_REQUIRED = ("api_url", "username", "password")

WAZUH = register_descriptor(
    IntegrationDescriptor(
        id="wazuh",
        category="SIEM",
        mcp_server_names=("wazuh",),
        fields=(
            IntegrationField("api_url"),
            IntegrationField("username"),
            IntegrationField("password", secret=True),
            # Unset verifies: skipping it sends the API password to whoever answers.
            IntegrationField("verify_ssl", value_type="bool", default=True),
            IntegrationField("ca_cert_path"),
        ),
    )
)
