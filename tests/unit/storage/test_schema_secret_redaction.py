"""Fields the serialization schemas must never expose.

Parity against the golden capture proves the shape didn't change; these tests
state *why* certain columns are absent, so widening a schema later fails with
a message about credentials rather than a diff against a fixture.
"""

import pytest

from core.storage.models import Finding, LLMProviderConfig, User
from core.storage.schemas.ai import LLMProviderConfigSchema
from core.storage.schemas.auth import UserSchema
from core.storage.schemas.finding import FindingSchema
from tests.unit.storage.orm_sample_instances import build_populated

pytestmark = pytest.mark.unit

# Columns on User that must not reach a response under any dump variant.
USER_SECRETS = (
    "password_hash",
    "password_history",
    "password_changed_at",
    "mfa_secret",
    "mfa_recovery_codes",
    "failed_login_count",
    "locked_until",
)


def test_user_schema_withholds_credentials():
    user = build_populated("User", User)
    dumped = UserSchema.dump(user)

    leaked = [name for name in USER_SECRETS if name in dumped]
    assert not leaked, f"UserSchema exposed credential fields: {leaked}"


def test_user_schema_still_reports_identity():
    """The redaction must not have gutted the useful fields."""
    user = build_populated("User", User)
    dumped = UserSchema.dump(user)

    for name in ("user_id", "username", "email", "role_id", "is_active"):
        assert name in dumped


def test_provider_dump_redacts_key_reference_by_default():
    provider = build_populated("LLMProviderConfig", LLMProviderConfig)
    dumped = LLMProviderConfigSchema.dump(provider)

    assert dumped["api_key_ref"] is None
    assert dumped["has_api_key"] is True, "presence flag must survive redaction"


def test_provider_dump_many_inherits_the_redacting_default():
    """Bulk serialization must not bypass the redaction."""
    provider = build_populated("LLMProviderConfig", LLMProviderConfig)

    (dumped,) = LLMProviderConfigSchema.dump_many([provider])

    assert dumped["api_key_ref"] is None


def test_provider_key_reference_requires_asking_explicitly():
    provider = build_populated("LLMProviderConfig", LLMProviderConfig)
    dumped = LLMProviderConfigSchema.dump_with_secrets(provider)

    assert dumped["api_key_ref"] == provider.api_key_ref
    assert dumped["api_key_ref"] is not None


def test_provider_without_a_key_reports_absence():
    provider = LLMProviderConfig(provider_id="p1", api_key_ref=None)
    dumped = LLMProviderConfigSchema.dump(provider)

    assert dumped["has_api_key"] is False


def test_finding_summary_omits_the_embedding_vector():
    """List responses must not carry the vector — it is large, not secret."""
    finding = build_populated("Finding", Finding)

    assert "embedding" not in FindingSchema.dump_summary(finding)
    assert "embedding" in FindingSchema.dump(finding)


def test_finding_summary_omission_survives_bulk_serialization():
    finding = build_populated("Finding", Finding)

    (dumped,) = FindingSchema.dump_many_summary([finding])

    assert "embedding" not in dumped
