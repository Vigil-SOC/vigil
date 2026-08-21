from __future__ import annotations

import pytest

from core.ingestion.ingestion_service import IngestionService

pytestmark = pytest.mark.unit


@pytest.fixture
def service():
    instance = IngestionService.__new__(IngestionService)
    instance._identity_warned = set()
    return instance


def _row(**overrides):
    row = {
        "sequence_id": "seq-1",
        "event_start_time": 1_785_000_000_000,
        "event_end_time": 1_785_000_060_000,
        "focal_ip": "10.0.0.1",
        "engaged_ip": "10.0.0.2",
        "embedding": [0.1, 0.2],
        "incident_pred": 0,
        "confidence_score": 0.93,
        "malicious": False,
        "label": "Benign",
    }
    row.update(overrides)
    return row


@pytest.mark.parametrize(
    ("incident_pred", "malicious", "label", "expected_match"),
    [
        (1, False, "Benign", False),
        (0, True, "Attack", False),
        (1, True, "Attack", True),
    ],
)
def test_prediction_and_ground_truth_are_kept_separate(
    service, incident_pred, malicious, label, expected_match
):
    finding = service._parquet_row_to_finding(
        _row(
            incident_pred=incident_pred,
            malicious=malicious,
            label=label,
        )
    )

    context = finding["entity_context"]
    assert context["verdict"] == ("attack" if incident_pred else "benign")
    assert context["prediction_confidence"] == pytest.approx(0.93)
    assert context["ground_truth_malicious"] is malicious
    assert context["ground_truth_verdict"] == (
        "attack" if malicious else "benign"
    )
    assert context["ground_truth_source"] == "malicious"
    assert context["prediction_matches_ground_truth"] is expected_match
    assert context["malicious"] is malicious
    assert context["label"] == label


def test_label_only_ground_truth_is_preserved(service):
    finding = service._parquet_row_to_finding(
        _row(malicious=None, label="Attack", incident_pred=0)
    )

    context = finding["entity_context"]
    assert context["ground_truth_malicious"] is True
    assert context["ground_truth_source"] == "label"
    assert context["prediction_matches_ground_truth"] is False


def test_contradictory_source_labels_are_rejected(service):
    with pytest.raises(ValueError, match="Contradictory ground truth"):
        service._parquet_row_to_finding(
            _row(malicious=False, label="Attack")
        )
