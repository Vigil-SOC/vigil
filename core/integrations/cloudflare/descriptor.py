"""Cloudflare integration descriptor — source of truth for registry entries."""

from core.integrations._base.descriptor import (
    IntegrationDescriptor,
    IntegrationField,
    register_descriptor,
)

CLOUDFLARE = register_descriptor(
    IntegrationDescriptor(
        id="cloudflare",
        category="Network Security",
        fields=(IntegrationField("api_token", secret=True),),
    )
)
