"""Amazon Security Lake descriptor — source of truth for its registry entries."""

from core.integrations._base.descriptor import (
    IntegrationDescriptor,
    IntegrationField,
    register_descriptor,
)

# No access-key field: credentials come from the default AWS chain of the process
# the server runs in, optionally assuming role_arn (with external_id).
AMAZON_SECURITY_LAKE = register_descriptor(
    IntegrationDescriptor(
        id="amazon-security-lake",
        category="SIEM",
        mcp_server_names=("amazon-security-lake",),
        fields=(
            IntegrationField("role_arn"),
            IntegrationField("external_id"),
            IntegrationField("workgroup", default="primary"),
            # Comma-separated region codes whose result rows may reach Vigil.
            # Empty means none: every other region answers a pointer only.
            IntegrationField("allowed_regions", default=""),
        ),
    )
)
