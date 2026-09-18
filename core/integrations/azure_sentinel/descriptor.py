"""Azure Sentinel descriptor — source of truth for its registry entries.

The field set below must match what ``ingestion.py`` actually reads: it used
to declare ``workspace_id, tenant_id, client_id, client_secret`` while
``fetch_alerts`` read ``tenant_id, client_id, client_secret, subscription_id,
resource_group, workspace_name`` and guarded on ``all([...])`` of those six.
Three of the six had no way to be populated through the Settings UI, so the
guard could never pass and ``fetch_alerts`` always returned ``[]`` — Azure
Sentinel ingestion could not work at all. ``subscription_id``,
``resource_group`` and ``workspace_name`` identify the Log Analytics workspace
that ``SecurityInsights.incidents.list`` is scoped to; ``workspace_id`` (the
workspace GUID) was never one of the values that call takes, so it is dropped
rather than carried along unused.
"""

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
