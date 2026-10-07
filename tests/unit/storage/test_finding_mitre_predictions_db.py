"""Persistence, lookup, and migration for finding_mitre_predictions."""

from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.dialects import postgresql

from core.reporting.analytics_service import get_mitre_technique_distribution
from core.storage.models import FindingMitrePrediction
from core.storage.schemas.finding import FindingSchema
from core.storage.service import DatabaseService, findings_by_technique_stmt
from core.time import utcnow

pytestmark = [pytest.mark.unit, pytest.mark.external_service, pytest.mark.database]

SQL_FILE = (
    Path(__file__).resolve().parents[3]
    / "infra"
    / "database"
    / "init"
    / "24_finding_mitre_predictions.sql"
)


def _create(service: DatabaseService, finding_id: str, predictions: dict, **kwargs):
    return service.create_finding(
        finding_id=finding_id,
        mitre_predictions=predictions,
        anomaly_score=kwargs.get("anomaly_score", 0.4),
        timestamp=kwargs.get("timestamp", utcnow()),
        data_source=kwargs.get("data_source", "test"),
        severity=kwargs.get("severity", "high"),
        status=kwargs.get("status", "new"),
    )


def test_create_update_and_bulk_persist_numeric_rows_only():
    service = DatabaseService()
    created = _create(
        service,
        "f-mitre-write-1",
        {
            "T1071.001": 0.875,
            "Initial Access": 0.5,
            "nested": {"nope": 1},
            "label": "ransomware",
        },
    )
    dumped = FindingSchema.dump(created)
    assert dumped["mitre_predictions"] == {
        "T1071.001": 0.875,
        "Initial Access": 0.5,
    }

    reread = FindingSchema.dump(service.get_finding("f-mitre-write-1"))
    assert reread["mitre_predictions"] == dumped["mitre_predictions"]

    assert service.update_finding(
        "f-mitre-write-1", mitre_predictions={"T1048.003": 0.25}
    )
    replaced = FindingSchema.dump(service.get_finding("f-mitre-write-1"))
    assert replaced["mitre_predictions"] == {"T1048.003": 0.25}

    assert service.update_finding(
        "f-mitre-write-1",
        mitre_predictions={"T1071.001": 0.25},
        severity="low",
        predicted_techniques=[{"technique_id": "T1071.001"}],
        unknown_s3_field=True,
    )
    whole_dict = FindingSchema.dump(service.get_finding("f-mitre-write-1"))
    assert whole_dict["mitre_predictions"] == {"T1071.001": 0.25}
    assert whole_dict["severity"] == "low"

    bulk = service.bulk_create_findings(
        [
            {
                "finding_id": "f-mitre-bulk-1",
                "mitre_predictions": {"T1059.001": 0.91},
                "anomaly_score": 0.2,
                "timestamp": utcnow(),
                "data_source": "test",
            }
        ]
    )
    assert bulk["imported"] == 1
    assert FindingSchema.dump(service.get_finding("f-mitre-bulk-1"))[
        "mitre_predictions"
    ] == {"T1059.001": 0.91}


def test_get_findings_by_technique_orders_by_confidence_past_the_old_cap():
    service = DatabaseService()
    now = utcnow()
    for i in range(12):
        _create(
            service,
            f"f-mitre-cap-{i:03d}",
            {"T1573.001": 0.1 + (i * 0.01)},
            timestamp=now - timedelta(seconds=i),
        )
    _create(service, "f-mitre-cap-other", {"T1110": 0.99})

    matches = service.get_findings_by_technique("T1573.001")
    assert len(matches) == 12
    confidences = [
        FindingSchema.dump(f)["mitre_predictions"]["T1573.001"] for f in matches
    ]
    assert confidences == sorted(confidences, reverse=True)
    assert "f-mitre-cap-other" not in {f.finding_id for f in matches}


def test_query_plan_uses_technique_confidence_index():
    service = DatabaseService()
    now = utcnow()
    for i in range(400):
        _create(
            service,
            f"f-mitre-plan-{i:04d}",
            {
                "T1071.001": 0.2 if i % 10 else 0.9,
                f"T1059.{i % 7:03d}": 0.3,
            },
            timestamp=now - timedelta(seconds=i),
        )

    manager = service.db_manager
    with manager.session_scope() as session:
        session.execute(text("ANALYZE finding_mitre_predictions"))
        session.execute(text("SET LOCAL enable_seqscan = off"))
        compiled = findings_by_technique_stmt("T1071.001").compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
        plan = "\n".join(
            row[0] for row in session.execute(text(f"EXPLAIN {compiled}")).all()
        )

    assert "idx_finding_mitre_predictions_technique_confidence" in plan


def test_migration_sql_backfills_numeric_values_only_and_drops_column():
    service = DatabaseService()
    _create(service, "f-mitre-migrate-1", {})

    sql = SQL_FILE.read_text()
    with service.db_manager.session_scope() as session:
        session.execute(
            text(
                "ALTER TABLE findings ADD COLUMN IF NOT EXISTS mitre_predictions jsonb"
            )
        )
        session.execute(
            text("""
                UPDATE findings
                SET mitre_predictions = CAST(:payload AS jsonb)
                WHERE finding_id = :fid
                """),
            {
                "fid": "f-mitre-migrate-1",
                "payload": (
                    '{"T1071.001": 0.875, "Initial Access": 0.5,'
                    ' "nested": {"x": 1}, "label": "ransomware"}'
                ),
            },
        )
        session.execute(
            text("DELETE FROM finding_mitre_predictions WHERE finding_id = :fid"),
            {"fid": "f-mitre-migrate-1"},
        )
        session.connection().exec_driver_sql(sql)

        rows = (
            session.query(FindingMitrePrediction)
            .filter(FindingMitrePrediction.finding_id == "f-mitre-migrate-1")
            .all()
        )
        by_id = {row.technique_id: row.confidence for row in rows}
        assert by_id == {"T1071.001": 0.875, "Initial Access": 0.5}
        columns = (
            session.execute(
                text(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_name = 'findings'"
                )
            )
            .scalars()
            .all()
        )
        assert "mitre_predictions" not in columns


@pytest.mark.asyncio
async def test_mitre_distribution_groups_child_table_rows():
    service = DatabaseService()
    now = utcnow()
    _create(service, "f-mitre-dist-1", {"T9998.001": 0.8}, timestamp=now)
    _create(
        service,
        "f-mitre-dist-2",
        {"T9998.001": 0.7, "T9998.002": 0.6},
        timestamp=now,
    )
    with service.db_manager.session_scope() as session:
        result = await get_mitre_technique_distribution(
            session,
            now - timedelta(hours=1),
            now + timedelta(hours=1),
            limit=50,
        )
    by_id = {row["techniqueId"]: row["count"] for row in result}
    assert by_id["T9998.001"] == 2
    assert by_id["T9998.002"] == 1
