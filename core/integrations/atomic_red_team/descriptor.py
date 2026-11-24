"""Atomic Red Team integration descriptor — source of truth for its registry entries."""

from core.integrations._base.descriptor import (
    IntegrationDescriptor,
    IntegrationField,
    register_descriptor,
)

ATOMIC_RED_TEAM = register_descriptor(
    IntegrationDescriptor(
        id="atomic-red-team",
        category="Forensics & Analysis",
        mcp_server_names=("atomic-red-team",),
        fields=(
            IntegrationField("runner_path"),
            IntegrationField("atomics_path"),
        ),
    )
)
