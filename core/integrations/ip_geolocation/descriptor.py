"""Ip Geolocation descriptor — source of truth for its registry entries."""

from core.integrations._base.descriptor import (
    IntegrationDescriptor,
    IntegrationField,
    register_descriptor,
)

IP_GEOLOCATION = register_descriptor(
    IntegrationDescriptor(
        id="ip-geolocation",
        category="Threat Intelligence",
        mcp_server_names=("ip-geolocation",),
        fields=(IntegrationField("api_key", secret=True),),
    )
)
