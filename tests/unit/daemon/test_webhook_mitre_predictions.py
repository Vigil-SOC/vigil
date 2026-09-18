"""/ingest coerces ``mitre_predictions`` to the canonical dict once at the
boundary (#966); every downstream consumer keeps assuming ``{technique: score}``.
"""

import pytest

from services.daemon.poller import normalize_mitre_predictions


@pytest.mark.parametrize(
    "raw, expected",
    [
        (None, {}),
        ({"T1486": 0.9, "T1490": 0.8}, {"T1486": 0.9, "T1490": 0.8}),
        (["T1486", "T1490"], {"T1486": 1.0, "T1490": 1.0}),
        (("T1486",), {"T1486": 1.0}),
        ("T1486", {"T1486": 1.0}),
        (
            [
                {"technique": "T1486", "confidence": 0.7},
                {"id": "T1490", "score": 0.4},
                {"technique": "T1059"},
                {"technique": "T1078", "confidence": "high"},
                {"technique": "T1105", "confidence": True},
            ],
            {"T1486": 0.7, "T1490": 0.4, "T1059": 1.0, "T1078": 1.0, "T1105": 1.0},
        ),
    ],
)
def test_accepted_shapes_become_technique_score_dict(raw, expected):
    assert normalize_mitre_predictions(raw, "f-1") == expected


@pytest.mark.parametrize(
    "raw",
    [42, 1.5, True, [{"confidence": 0.9}], [["T1486"]], [""], [None]],
)
def test_uncoercible_shapes_raise_naming_field_and_finding(raw):
    with pytest.raises(ValueError, match=r"finding repro-001: mitre_predictions"):
        normalize_mitre_predictions(raw, "repro-001")
