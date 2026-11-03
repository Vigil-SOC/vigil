"""Tests for service initialization and schema revision caching (#3666)."""

from pathlib import Path
from unittest.mock import MagicMock, patch

from core.storage import connection
from core.storage.database_data_service import get_database_data_service
from core.workflows import workflows_service
from core.workflows.workflows_service import WorkflowsService


def test_require_schema_revision_memoizes_verified_engine() -> None:
    """_require_schema_revision should query the DB only once per verified engine."""
    connection.reset_schema_drift_check()
    head = connection.schema_head()
    assert head

    fake_engine = MagicMock()
    mock_conn = MagicMock()
    mock_conn.execute.return_value.scalar.return_value = head
    fake_engine.connect.return_value.__enter__.return_value = mock_conn

    with patch.dict("os.environ", {"VIGIL_AUTO_UPGRADE": "0"}, clear=False):
        connection._require_schema_revision(fake_engine)
        connection._require_schema_revision(fake_engine)

    assert fake_engine.connect.call_count == 1
    connection.reset_schema_drift_check()


def test_get_database_data_service_returns_singleton() -> None:
    """get_database_data_service should return a cached DatabaseDataService instance."""
    with patch("core.storage.database_data_service.DatabaseDataService") as mock_cls:
        import core.storage.database_data_service as dds_mod

        dds_mod._default_service = None
        first = get_database_data_service()
        second = get_database_data_service()

        assert first is second
        assert mock_cls.call_count == 1
        dds_mod._default_service = None


def test_workflows_service_reuses_bundled_cache(tmp_path: Path) -> None:
    """Repeated WorkflowsService instantiations on unchanged files should reuse cache."""
    wf_dir = tmp_path / "sample_wf"
    wf_dir.mkdir()
    (wf_dir / "WORKFLOW.md").write_text(
        "---\nname: Sample Workflow\ndescription: desc\n---\nSteps here.\n",
        encoding="utf-8",
    )

    WorkflowsService._BUNDLED_CACHE.clear()
    with patch(
        "core.workflows.workflows_service._read_workflow_file",
        wraps=workflows_service._read_workflow_file,
    ) as mock_parse:
        svc1 = WorkflowsService(
            workflows_dir=tmp_path,
            custom_workflows=MagicMock(),
            workflow_runs=MagicMock(),
        )
        svc2 = WorkflowsService(
            workflows_dir=tmp_path,
            custom_workflows=MagicMock(),
            workflow_runs=MagicMock(),
        )

        assert "sample_wf" in svc1._cache
        assert "sample_wf" in svc2._cache
        assert mock_parse.call_count == 1
