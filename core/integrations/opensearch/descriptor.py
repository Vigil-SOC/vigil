"""OpenSearch integration descriptor — source of truth for its registry entries."""

from core.integrations._base.descriptor import (
    IntegrationDescriptor,
    IntegrationField,
    register_descriptor,
)

OPENSEARCH = register_descriptor(
    IntegrationDescriptor(
        id="opensearch",
        category="SIEM",
        mcp_server_names=("opensearch",),
        fields=(
            IntegrationField("opensearch_url"),
            IntegrationField("username"),
            IntegrationField("password", secret=True),
            IntegrationField("dashboards_url"),
            IntegrationField("index_pattern"),
            IntegrationField("verify_ssl", value_type="bool"),
            IntegrationField("ca_cert_path"),
        ),
    )
)
