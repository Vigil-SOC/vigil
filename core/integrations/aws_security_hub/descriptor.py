"""AWS Security Hub integration descriptor — source of truth for registry entries."""

from core.integrations._base.descriptor import (
    IntegrationDescriptor,
    IntegrationField,
    register_descriptor,
)

AWS_SECURITY_HUB = register_descriptor(
    IntegrationDescriptor(
        id="aws-security-hub",
        category="Cloud Security",
        mcp_server_name="aws-security-hub-server",
        fields=(IntegrationField("secret_access_key", secret=True),),
    )
)
