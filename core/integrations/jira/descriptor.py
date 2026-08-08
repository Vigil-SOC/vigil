"""Jira integration descriptor — source of truth for registry entries."""

from core.integrations._base.descriptor import (
    IntegrationDescriptor,
    IntegrationField,
    register_descriptor,
)

JIRA = register_descriptor(
    IntegrationDescriptor(
        id="jira",
        category="Incident Management",
        mcp_server_name="jira-server",
        fields=(IntegrationField("api_token", secret=True),),
    )
)
