"""The migrations form one chain: two heads stop the backend from starting."""

from core.upgrade.command import script_directory


def test_migrations_have_a_single_head():
    heads = script_directory().get_heads()
    assert len(heads) == 1, f"parallel migrations, chain them into one: {heads}"
