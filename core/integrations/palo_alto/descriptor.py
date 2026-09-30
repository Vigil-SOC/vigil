"""Palo Alto integration descriptor — source of truth for its registry entries."""

from core.integrations._base.descriptor import (
    IntegrationDescriptor,
    IntegrationField,
    register_descriptor,
)

PALO_ALTO = register_descriptor(
    IntegrationDescriptor(
        id="palo-alto",
        category="Network Security",
        mcp_server_names=("palo-alto",),
        fields=(
            IntegrationField("hostname"),
            IntegrationField("api_key", secret=True),
            IntegrationField("verify_ssl", value_type="bool"),
            IntegrationField("ca_cert_path"),
        ),
    )
)
