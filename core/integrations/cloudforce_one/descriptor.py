"""Cloudforce One integration descriptor — source of truth for its registry entries.

The poller and the credential check both resolve this descriptor. There is no
MCP server: Vigil pulls the TAXII collections itself.
"""

from core.integrations._base.descriptor import (
    IntegrationDescriptor,
    IntegrationField,
    register_descriptor,
)

CLOUDFORCE_ONE = register_descriptor(
    IntegrationDescriptor(
        id="cloudforce_one",
        category="Threat Intelligence",
        fields=(
            IntegrationField("api_token", secret=True),
            IntegrationField("taxii_server_url"),
            IntegrationField("collection_ids"),
            IntegrationField("poll_interval_seconds", value_type="int"),
        ),
    )
)
