"""Azure Sentinel descriptor — source of truth for its registry entries."""

from core.integrations._base.descriptor import (
    IntegrationDescriptor,
    IntegrationField,
    register_descriptor,
)

AZURE_SENTINEL = register_descriptor(
    IntegrationDescriptor(
        id="azure-sentinel",
        category="SIEM",
        mcp_server_names=("azure-sentinel",),
        fields=(
            IntegrationField("tenant_id"),
            IntegrationField("client_id"),
            IntegrationField("client_secret", secret=True),
            IntegrationField("subscription_id"),
            IntegrationField("resource_group"),
            IntegrationField("workspace_name"),
        ),
    )
)
