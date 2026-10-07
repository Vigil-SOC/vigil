"""Finding and reading the default-row seed, without a database."""

import re
from pathlib import Path

import pytest

from core.storage.reference_seed import (
    SEED_FILE,
    find_seed_file,
    inserts_by_table,
    split_statements,
)

pytestmark = pytest.mark.unit

SEED = Path(__file__).resolve().parents[3] / "infra" / "database" / "init" / SEED_FILE


@pytest.mark.parametrize("layout", ["infra/database/init", "database/init"])
def test_finds_the_seed_in_a_checkout_and_in_the_image(tmp_path, layout):
    # The backend image copies infra/database/init to /app/database/init.
    directory = tmp_path / layout
    directory.mkdir(parents=True)
    (directory / SEED_FILE).write_text("", encoding="utf-8")
    assert find_seed_file(tmp_path) == directory / SEED_FILE


def test_a_tree_without_the_seed_finds_none(tmp_path):
    assert find_seed_file(tmp_path) is None


def test_the_repository_seed_is_the_one_found():
    assert find_seed_file() == SEED


def test_inserts_are_grouped_by_table_in_file_order():
    # Policies first: a template names its policy.
    sql = SEED.read_text(encoding="utf-8")
    grouped = inserts_by_table(sql)
    assert list(grouped) == ["sla_policies", "case_templates"]
    for table, statements in grouped.items():
        assert len(statements) == len(re.findall(rf"INSERT INTO {table}\b", sql))


def test_semicolons_in_quotes_and_dollar_quotes_do_not_split():
    statements = [
        "INSERT INTO t (a) VALUES ('x;y')",
        "DO $body$ BEGIN PERFORM 1; END $body$",
    ]
    assert list(split_statements(";\n".join(statements) + ";\n")) == statements
