"""Slack integration descriptor — source of truth for registry entries."""

from core.integrations._base.descriptor import (
    IntegrationDescriptor,
    IntegrationField,
    register_descriptor,
)

SLACK = register_descriptor(
    IntegrationDescriptor(
        id="slack",
        category="Communications",
        mcp_server_name="slack-server",
        fields=(IntegrationField("bot_token", secret=True),),
    )
)
