"""Unit tests for core/integrations/azure_sentinel/ingestion.py.

Covers the two bugs fixed here:

1. The descriptor/ingestion field mismatch: ``descriptor.py`` used to declare
   ``workspace_id, tenant_id, client_id, client_secret`` while ``fetch_alerts``
   read six different fields (including three the descriptor never declared),
   so the completeness guard could never pass. ``test_required_fields_are_all_
   declared_on_the_descriptor`` pins the two lists together so they cannot
   drift apart again.
2. ``except ImportError: return []`` made a missing Azure SDK indistinguishable
   from an empty result set. ``test_missing_sdk_raises_instead_of_returning_
   empty`` pins the fix: a missing SDK now raises with an install hint.
"""

from unittest.mock import MagicMock, patch

import pytest

from core.integrations.azure_sentinel.descriptor import AZURE_SENTINEL
from core.integrations.azure_sentinel.ingestion import AzureSentinelIngestion

REQUIRED_FIELDS = (
    "tenant_id",
    "client_id",
    "client_secret",
    "subscription_id",
    "resource_group",
    "workspace_name",
)


@pytest.fixture
def ingestion():
    with patch(
        "core.integrations.azure_sentinel.ingestion.get_integration_config"
    ) as mock_config:
        mock_config.return_value = {}
        svc = AzureSentinelIngestion()
        yield svc


@pytest.fixture
def stub_azure_sdk():
    """Stand in for the azure-identity / azure-mgmt-securityinsight packages,
    which are not declared as a dependency upstream, so tests that need the
    deferred import inside fetch_alerts() to succeed must fake it out."""
    import sys
    import types

    identity_mod = types.ModuleType("azure.identity")
    identity_mod.ClientSecretCredential = MagicMock()
    securityinsight_mod = types.ModuleType("azure.mgmt.securityinsight")
    securityinsight_mod.SecurityInsights = MagicMock()
    azure_mod = types.ModuleType("azure")
    mgmt_mod = types.ModuleType("azure.mgmt")

    with patch.dict(
        sys.modules,
        {
            "azure": azure_mod,
            "azure.identity": identity_mod,
            "azure.mgmt": mgmt_mod,
            "azure.mgmt.securityinsight": securityinsight_mod,
        },
    ):
        yield


def test_required_fields_are_all_declared_on_the_descriptor():
    """Every field fetch_alerts() reads must be one the descriptor declares.

    This is the regression the upstream bug hinged on: three of the six
    fields ingestion read had no way to be populated because the descriptor
    (and, transitively, the Settings form) never offered them.
    """
    declared = set(AZURE_SENTINEL.field_names)
    assert set(REQUIRED_FIELDS) <= declared, (
        "AzureSentinelIngestion.fetch_alerts() reads a field the descriptor "
        f"does not declare: missing {set(REQUIRED_FIELDS) - declared}"
    )


@pytest.mark.asyncio
async def test_incomplete_config_returns_empty_without_raising(
    ingestion, stub_azure_sdk
):
    ingestion.config = {"tenant_id": "t", "client_id": "c"}
    alerts = await ingestion.fetch_alerts()
    assert alerts == []


@pytest.mark.asyncio
async def test_missing_sdk_raises_instead_of_returning_empty(ingestion):
    """A missing azure-identity/azure-mgmt-securityinsight install must be
    loud, not indistinguishable from "no incidents found"."""
    ingestion.config = {field: "x" for field in REQUIRED_FIELDS}

    with patch("builtins.__import__", side_effect=ImportError("no module")):
        with pytest.raises(RuntimeError, match="Azure SDK not installed"):
            await ingestion.fetch_alerts()
