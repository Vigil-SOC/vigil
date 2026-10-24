"""POST /ingest-string parses JSON, JSONL and CSV like the file ingesters."""

import pytest

from core.ingestion.ingestion_service import IngestionService

pytestmark = pytest.mark.unit


class _CaptureDb:
    def __init__(self):
        self.batch = []
        self.cases = []

    def bulk_create_findings(self, rows):
        self.batch.extend(rows)
        return {"imported": len(rows), "skipped": 0}

    def get_case(self, case_id):
        return None

    def create_case(self, case_id, **kwargs):
        self.cases.append(case_id)
        return {"case_id": case_id}


def _svc():
    s = IngestionService()
    s.use_database = True
    s.db_service = _CaptureDb()
    return s


def _outcome(svc, stats):
    return (
        dict(stats),
        sorted(r["finding_id"] for r in svc.db_service.batch),
        sorted(svc.db_service.cases),
    )


TEMPO_HEADER = (
    "sequence_id,attack_id,IP1,IP2,mitre_tactic,incident_confidence,event_start\n"
)
TEMPO_ROW = "a,1.1.1.1,2.2.2.2,Execution,90,2024-01-01T00:00:00Z\n"

CASES = {
    "json_list_keyed_by_id": (
        '[{"id":"a-1","title":"t"},{"id":"a-2","title":"t"}]',
        "json",
        "finding",
    ),
    "json_wrapper": (
        '{"findings":[{"finding_id":"w-1"}],"cases":[{"case_id":"wc-1"}]}',
        "json",
        "finding",
    ),
    "jsonl_one_bad_line": (
        '{"finding_id":"b-1"}\nnot json\n{"finding_id":"b-2"}\n',
        "jsonl",
        "finding",
    ),
    "jsonl_cases_one_bad_line": (
        '{"case_id":"k-1"}\nnot json\n{"case_id":"k-2"}\n',
        "jsonl",
        "case",
    ),
    "csv_one_bad_row": (
        TEMPO_HEADER + "s1," + TEMPO_ROW + "s2,a\n" + "s3," + TEMPO_ROW,
        "csv",
        "finding",
    ),
    "csv_plain": ("finding_id,severity\nc-1,high\nc-2,low\n", "csv", "finding"),
}


@pytest.mark.parametrize("name", list(CASES))
def test_string_matches_file(name, tmp_path):
    body, fmt, data_type = CASES[name]
    from_string = _svc()
    string_stats = from_string.ingest_from_string(body, fmt, data_type)
    path = tmp_path / f"in.{fmt}"
    path.write_text(body)
    from_file = _svc()
    file_stats = from_file._ingest_file_by_format(path, fmt, data_type=data_type)

    outcome = _outcome(from_string, string_stats)
    assert outcome == _outcome(from_file, file_stats)
    assert outcome[1] or outcome[2]


@pytest.mark.parametrize(
    "body,data_type,findings,cases",
    [
        ('{"finding_id":"x-1","title":"t"}', "finding", ["x-1"], []),
        ('{"case_id":"cx-1"}', "case", [], ["cx-1"]),
        ('{"case_id":"cx-1"}', "finding", [], []),
    ],
)
def test_single_object_from_string(body, data_type, findings, cases):
    svc = _svc()
    stats = svc.ingest_from_string(body, "json", data_type)
    assert _outcome(svc, stats)[1:] == (findings, cases)


@pytest.mark.parametrize(
    "name,total,imported,errors",
    [
        ("json_list_keyed_by_id", 2, 2, 0),
        ("jsonl_one_bad_line", 2, 2, 1),
        ("csv_one_bad_row", 3, 2, 1),
    ],
)
def test_string_counts_good_rows_and_errors(name, total, imported, errors):
    body, fmt, data_type = CASES[name]
    svc = _svc()
    stats = svc.ingest_from_string(body, fmt, data_type)
    assert stats["findings_total"] == total
    assert stats["findings_imported"] == imported
    assert stats["findings_errors"] == errors
    assert len(svc.db_service.batch) == imported


def test_id_keyed_list_is_not_imported_as_cases():
    svc = _svc()
    stats = svc.ingest_from_string('[{"id":"c-1","title":"t"}]', "json", "case")
    assert stats["findings_total"] == 0
    assert svc.db_service.batch == []
    assert svc.db_service.cases == []
