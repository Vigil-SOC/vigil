"""Azure Sentinel integration descriptor — source of truth for registry entries."""

from core.integrations._base.descriptor import (
    IntegrationDescriptor,
    IntegrationField,
    register_descriptor,
)

AZURE_SENTINEL = register_descriptor(
    IntegrationDescriptor(
        id="azure-sentinel",
        category="SIEM",
        mcp_server_name="azure-sentinel-server",
        fields=(IntegrationField("client_secret", secret=True),),
    )
)
