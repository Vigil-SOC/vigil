"""Misp integration descriptor — source of truth for its registry entries."""

from core.integrations._base.descriptor import (
    IntegrationDescriptor,
    IntegrationField,
    register_descriptor,
)

MISP = register_descriptor(
    IntegrationDescriptor(
        id="misp",
        category="Threat Intelligence",
        mcp_server_names=("misp",),
        fields=(
            IntegrationField("url"),
            IntegrationField("api_key", secret=True),
            IntegrationField("verify_ssl", value_type="bool"),
            IntegrationField("ca_cert_path"),
        ),
    )
)
