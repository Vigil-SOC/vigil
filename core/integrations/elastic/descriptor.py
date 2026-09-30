"""Elastic integration descriptor — source of truth for Elastic's registry entries."""

from core.integrations._base.descriptor import (
    IntegrationDescriptor,
    IntegrationField,
    register_descriptor,
)

ELASTIC = register_descriptor(
    IntegrationDescriptor(
        id="elastic-siem",
        category="SIEM",
        mcp_server_names=("elastic",),
        fields=(
            IntegrationField("elasticsearch_url"),
            IntegrationField("api_key", secret=True),
            IntegrationField("username"),
            IntegrationField("password", secret=True),
            IntegrationField("kibana_url"),
            IntegrationField("index_pattern"),
            IntegrationField("min_rule_level", value_type="int"),
            IntegrationField("verify_ssl", value_type="bool"),
        ),
    )
)
