"""VStrike integration descriptor — source of truth for registry entries."""

from core.integrations._base.descriptor import (
    IntegrationDescriptor,
    IntegrationField,
    register_descriptor,
)

VSTRIKE = register_descriptor(
    IntegrationDescriptor(
        id="vstrike",
        category="Network Security",
        fields=(
            IntegrationField("username", secret=True),
            IntegrationField("password", secret=True),
        ),
    )
)
