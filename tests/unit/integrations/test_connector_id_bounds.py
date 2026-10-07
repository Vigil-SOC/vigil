"""Connector-built ids fit findings.finding_id / external_id (#1434)."""

from unittest.mock import patch

import pytest

from core.integrations._base.ids import fit_id
from core.integrations.aws_security_hub.ingestion import AWSSecurityHubIngestion
from core.integrations.azure_sentinel.ingestion import AzureSentinelIngestion

pytestmark = pytest.mark.unit

ARN = (
    "arn:aws:securityhub:us-east-1:123456789012:subscription/"
    "aws-foundational-security-best-practices/v/1.0.0/IAM.1/finding/{}"
)
ARM = (
    "/subscriptions/00000000-0000-0000-0000-000000000000/resourceGroups/rg/"
    "providers/Microsoft.OperationalInsights/workspaces/ws/providers/"
    "Microsoft.SecurityInsights/incidents/{}"
)


def test_fit_id_leaves_ids_that_fit_alone():
    assert fit_id("x-", "a" * 48, 50) == "x-" + "a" * 48


def test_fit_id_keeps_long_ids_with_a_shared_prefix_distinct():
    a, b = fit_id("x-", ARN.format("1"), 50), fit_id("x-", ARN.format("2"), 50)
    assert len(a) == len(b) == 50
    assert a != b


@pytest.mark.parametrize(
    "cls,module,prefix,key,template",
    [
        (AWSSecurityHubIngestion, "aws_security_hub", "aws-sh-", "Id", ARN),
        (AzureSentinelIngestion, "azure_sentinel", "sentinel-", "id", ARM),
    ],
)
def test_long_source_ids_fit_and_keep_full_external_id(
    cls, module, prefix, key, template
):
    with patch(f"core.integrations.{module}.ingestion.resolve", return_value={}):
        svc = cls()
    long_id, other_id = template.format("a" * 36), template.format("b" * 36)

    f1 = svc.transform_alert_to_finding({key: long_id})
    f2 = svc.transform_alert_to_finding({key: other_id})

    assert len(f1["finding_id"]) <= 50
    assert f1["finding_id"] != f2["finding_id"]
    assert f1["external_id"] == f"{prefix}{long_id}"

    short = svc.transform_alert_to_finding({key: "abc"})
    assert short["finding_id"] == f"{prefix}abc"
    assert short["external_id"] == f"{prefix}abc"
