"""VStrike integration descriptor — source of truth for its registry entries."""

from core.integrations._base.descriptor import (
    IntegrationDescriptor,
    IntegrationField,
    register_descriptor,
)

VSTRIKE = register_descriptor(
    IntegrationDescriptor(
        id="vstrike",
        category="Network Security",
        mcp_server_names=("vstrike",),
        fields=(
            IntegrationField("url"),
            # The catalog types username as text, but client.py reads it through
            # get_secret alongside the password for the /mcp-login exchange, so
            # it stays in the encrypted store.
            IntegrationField("username", secret=True),
            IntegrationField("password", secret=True),
            IntegrationField("verify_ssl", value_type="bool"),
            IntegrationField("ca_cert_path"),
        ),
    )
)
