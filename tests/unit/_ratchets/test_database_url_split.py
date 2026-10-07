"""DATABASE_URL is the agent's knob, not Python's (#752).

env.example used to call it the single source of truth. Settings bound it,
DaemonConfig copied it, and nothing in core/ or services/ read the copy.
A field that is only assigned is how the trap stayed invisible. These
assertions fail if that wiring comes back, or if Helm/docs claim a DSN
the chart does not assemble.
"""

from pathlib import Path

import pytest

from core.config import Settings
from services.daemon.config import DaemonConfig

REPO_ROOT = Path(__file__).resolve().parents[3]
ENV_EXAMPLE = REPO_ROOT / "env.example"
HELM_HELPERS = REPO_ROOT / "infra" / "helm" / "vigil" / "templates" / "_helpers.tpl"
HELM_VALUES = REPO_ROOT / "infra" / "helm" / "vigil" / "values.yaml"

pytestmark = pytest.mark.unit


def test_settings_does_not_bind_database_url(monkeypatch):
    monkeypatch.setenv(
        "DATABASE_URL", "postgresql://other:pw@agent-only.example/agent_db"
    )
    assert "database_url" not in Settings.model_fields
    assert "database_url" not in Settings().model_dump()


def test_daemon_config_does_not_copy_database_url():
    assert "database_url" not in DaemonConfig.__dataclass_fields__


def test_env_example_states_the_split_instead_of_a_single_source():
    text = ENV_EXAMPLE.read_text()
    assert "DATABASE_URL is the single source of truth" not in text
    assert "DATABASE_URL=" in text
    assert "services/agent/core/db.ts" in text
    assert "migrate_schema.py" in text


def test_helm_does_not_assemble_database_url():
    helpers = HELM_HELPERS.read_text()
    values = HELM_VALUES.read_text()
    assert "databaseUrlNoPassword" not in helpers
    assert "append ?sslmode=require to the DATABASE_URL" not in values
    assert "DATABASE_URL assembly" not in values
