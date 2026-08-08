"""Microsoft Defender integration descriptor — source of truth for registry entries."""

from core.integrations._base.descriptor import (
    IntegrationDescriptor,
    IntegrationField,
    register_descriptor,
)

MICROSOFT_DEFENDER = register_descriptor(
    IntegrationDescriptor(
        id="microsoft-defender",
        category="EDR",
        mcp_server_name="microsoft-defender-server",
        fields=(IntegrationField("client_secret", secret=True),),
    )
)
