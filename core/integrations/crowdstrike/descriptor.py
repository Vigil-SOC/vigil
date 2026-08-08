"""CrowdStrike integration descriptor — source of truth for its registry entries."""

from core.integrations._base.descriptor import (
    IntegrationDescriptor,
    IntegrationField,
    register_descriptor,
)

CROWDSTRIKE = register_descriptor(
    IntegrationDescriptor(
        id="crowdstrike",
        category="EDR",
        mcp_server_name="crowdstrike-server",
        fields=(
            IntegrationField("client_id"),
            IntegrationField("client_secret", secret=True),
            IntegrationField("base_url"),
        ),
    )
)
