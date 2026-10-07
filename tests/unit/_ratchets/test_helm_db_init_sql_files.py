"""Helm's db-init must apply every init SQL file that compose applies.

Compose's 00_apply.sh globs infra/database/init/*.sql. The chart's db-init Job
applies only the files named in dbInit.sqlFiles, in that order, recording each
filename in _vigil_schema_versions. The Helm Chart workflow diffs the SQL
directory against the chart's bundled copy, so a new file always reached the
bundle, but nothing compared the list: 34_drop_skills.sql (#1066) and
34_mcp_credentials.sql (#979) shipped in 0.6.0 bundled and never run. Helm
installs kept the retired skills table and left mcp_credentials for the
backend's create_all to build as vigil_app.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit

REPO = Path(__file__).resolve().parents[3]
INIT_SQL = REPO / "infra" / "database" / "init"
HELM_VALUES = REPO / "infra" / "helm" / "vigil" / "values.yaml"


def _sql_files() -> list[str]:
    values = yaml.safe_load(HELM_VALUES.read_text(encoding="utf-8"))
    return list(values["dbInit"]["sqlFiles"])


def _init_sql() -> list[str]:
    return sorted(p.name for p in INIT_SQL.glob("*.sql"))


def test_every_init_sql_file_is_in_sql_files() -> None:
    listed = set(_sql_files())
    missing = [name for name in _init_sql() if name not in listed]
    assert not missing, (
        f"Helm installs never run {missing}: add them to dbInit.sqlFiles in "
        "infra/helm/vigil/values.yaml"
    )


def test_every_sql_files_entry_exists() -> None:
    # The Job fails the release on an entry it cannot find in the bundle.
    present = set(_init_sql())
    unknown = [name for name in _sql_files() if name not in present]
    assert not unknown, f"dbInit.sqlFiles names files that do not exist: {unknown}"


def test_sql_files_are_in_the_order_compose_applies_them() -> None:
    # A shell glob expands in sorted filename order, so that is compose's order.
    listed = _sql_files()
    assert listed == sorted(set(listed)), "dbInit.sqlFiles is out of order or repeats"
