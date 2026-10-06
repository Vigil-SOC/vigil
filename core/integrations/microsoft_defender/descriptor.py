"""Microsoft Defender descriptor — source of truth for its registry entries."""

from core.integrations._base.descriptor import (
    IntegrationDescriptor,
    IntegrationField,
    register_descriptor,
)

MICROSOFT_DEFENDER = register_descriptor(
    IntegrationDescriptor(
        id="microsoft-defender",
        category="EDR",
        mcp_server_names=("microsoft-defender",),
        fields=(
            IntegrationField("tenant_id"),
            IntegrationField("client_id"),
            IntegrationField("client_secret", secret=True),
        ),
        # Portal alert page: the ``alertWebUrl`` example in
        # https://learn.microsoft.com/graph/api/resources/security-alert
        # (https://security.microsoft.com/alerts/<id>?tid=<tenant>). The id is
        # the Defender alert id Vigil stores as ``external_id``. Public cloud
        # only; sovereign-cloud portal hosts are not supported.
        console_link_template=(
            "https://security.microsoft.com/alerts/{external_id}?tid={tenant_id}"
        ),
    )
)
