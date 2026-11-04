"""OpenCTI integration descriptor — source of truth for its registry entries.

Read-only: observable, indicator and entity lookups over the GraphQL API.
"""

from core.integrations._base.descriptor import (
    IntegrationDescriptor,
    IntegrationField,
    register_descriptor,
)

OPENCTI_REQUIRED = ("url", "api_token")

OPENCTI = register_descriptor(
    IntegrationDescriptor(
        id="opencti",
        category="Threat Intelligence",
        mcp_server_names=("opencti",),
        fields=(
            IntegrationField("url"),
            IntegrationField("api_token", secret=True),
            # Unset verifies: skipping it sends the API token to whoever answers.
            IntegrationField("verify_ssl", value_type="bool", default=True),
            IntegrationField("ca_cert_path"),
        ),
    )
)
