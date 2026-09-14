"""Opening context must name the case the investigation already has (#867)."""

from services.daemon.plan_generator import generate_initial_context


def test_includes_case_id_when_the_investigation_has_one():
    text = generate_initial_context(
        [{"finding_id": "f-1", "severity": "high", "description": "lockouts"}],
        case_id="c-42",
    )
    assert "case_id: c-42" in text
    assert "f-1" in text


def test_omits_case_id_when_the_investigation_has_none():
    text = generate_initial_context(
        [{"finding_id": "f-1", "severity": "high", "description": "lockouts"}]
    )
    assert "case_id:" not in text
