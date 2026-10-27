"""/ingest coerces ``mitre_predictions`` to ``{technique: score or null}`` once
at the boundary (#966, #3221). A missing score is null, not 1.0.
"""

import pytest

from services.daemon.poller import normalize_mitre_predictions


@pytest.mark.parametrize(
    "raw, expected",
    [
        (None, {}),
        ({}, {}),
        ([], {}),
        ({"T1486": 0.9, "T1490": 0.8}, {"T1486": 0.9, "T1490": 0.8}),
        ({"T1486": 1.5, "T1490": "2"}, {"T1486": 1.5, "T1490": 2.0}),
        (["T1486", "T1490"], {"T1486": None, "T1490": None}),
        (("T1486",), {"T1486": None}),
        ("T1486", {"T1486": None}),
        (
            [
                {"technique": "T1486", "confidence": 0.7},
                {"id": "T1490", "score": 0.4},
                {"technique": "T1059"},
                {"technique": "T1078", "confidence": "high"},
                {"technique": "T1105", "confidence": True},
                {"technique": None, "id": "T1021", "confidence": None, "score": 0.3},
            ],
            {
                "T1486": 0.7,
                "T1490": 0.4,
                "T1059": None,
                "T1078": None,
                "T1105": None,
                "T1021": 0.3,
            },
        ),
    ],
)
def test_accepted_shapes_become_technique_score_dict(raw, expected):
    assert normalize_mitre_predictions(raw, "f-1") == expected


def test_bool_and_bad_string_warn_and_a_list_value_is_skipped(caplog):
    result = normalize_mitre_predictions(
        {"T1059": None, "T1071": "0.4", "T1000": True, "nested": [1, 2]},
        "f-1",
        data_source="webhook",
    )
    assert result == {"T1059": None, "T1071": 0.4, "T1000": None}
    assert "T1059" not in caplog.text
    assert "webhook" in caplog.text
    assert "True" in caplog.text
    assert "nested" in caplog.text
    assert "skipped" in caplog.text


@pytest.mark.parametrize(
    "raw",
    [42, 1.5, True, [{"confidence": 0.9}], [["T1486"]], [""], [None]],
)
def test_uncoercible_shapes_raise_naming_field_and_finding(raw):
    with pytest.raises(ValueError, match=r"finding repro-001: mitre_predictions"):
        normalize_mitre_predictions(raw, "repro-001")
