import pytest

from services.daemon.workdir import WorkdirManager

pytestmark = pytest.mark.unit


@pytest.fixture
def wm(tmp_path):
    (tmp_path / "secret.env").write_text("DB_PASSWORD=x")
    wm = WorkdirManager(str(tmp_path / "investigations"))
    wm.create("inv-1")
    wm.create("inv-10")
    wm.write_file("inv-10", "context.md", "INV-10 BRIEF")
    return wm


def test_reads_its_own_files_including_nested_ones(wm):
    assert wm.write_file("inv-1", "evidence/query_results/q1.json", "[1]")
    assert wm.read_file("inv-1", "evidence/query_results/q1.json") == "[1]"
    assert wm.write_file("inv-1", "context.md", "mine")
    assert wm.read_file("inv-1", "./evidence/../context.md") == "mine"


def test_cannot_reach_a_sibling_whose_id_shares_the_prefix(wm):
    assert wm.read_file("inv-1", "../inv-10/context.md") == ""
    assert not wm.write_file("inv-1", "../inv-10/context.md", "overwritten")
    assert not wm.append_file("inv-1", "../inv-10/context.md", "appended")
    assert wm.read_file("inv-10", "context.md") == "INV-10 BRIEF"


@pytest.mark.parametrize("investigation_id", ["..", ".", "inv-1/..", "inv-1/evidence"])
def test_an_id_that_is_not_one_directory_under_the_base_is_refused(
    wm, tmp_path, investigation_id
):
    assert wm.read_file(investigation_id, "secret.env") == ""
    assert wm.read_file(investigation_id, "../secret.env") == ""
    assert not wm.write_file(investigation_id, "state.json", "{}")
    wm.write_state(investigation_id, {"status": "failed"})
    assert not (tmp_path / "state.json").exists()
    assert not (tmp_path / "investigations" / "state.json").exists()


def test_plain_traversal_out_of_the_base_is_refused(wm):
    assert wm.read_file("inv-1", "../../secret.env") == ""
