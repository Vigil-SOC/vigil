"""Elastic Security integration descriptor — source of truth for registry entries."""

from core.integrations._base.descriptor import (
    IntegrationDescriptor,
    IntegrationField,
    register_descriptor,
)

ELASTIC = register_descriptor(
    IntegrationDescriptor(
        id="elastic-siem",
        category="SIEM",
        fields=(
            IntegrationField("api_key", secret=True),
            IntegrationField("password", secret=True),
        ),
    )
)
