"""The rule text and lint steps, no database: what a reviewer reads off the checks
(#4157). Ported from 1.x's tests/unit/detection_engineering/test_rule_text.py,
less its pySigma conversion tests and its checks against stored events (main keeps
no log store)."""

import pytest

from core.detections.engineering import lint, sigma_text
from core.detections.replay import replay_sigma

from .conftest import RULE


def _checks(report):
    return {check["name"]: check for check in report["checks"]}


def test_fill_in_adds_what_a_reviewer_would_and_keeps_an_id_across_edits():
    text, fixes = sigma_text.fill_in(RULE, "T1059.001")
    doc = sigma_text.parse(text)
    assert "added id" in fixes
    assert doc["tags"] == ["attack.execution", "attack.t1059.001"]
    assert sigma_text.fill_in(text, "T1059.001") == (text, [])


def test_fill_in_leaves_text_that_is_no_rule_for_lint_to_refuse():
    assert sigma_text.fill_in("not: [valid", None) == ("not: [valid", [])
    assert sigma_text.fill_in("a: 1\n---\nb: 2\n", None) == ("a: 1\n---\nb: 2\n", [])


def test_a_rule_keyed_to_one_address_is_refused_by_vigils_lint():
    text = RULE.replace("CommandLine|contains: '-enc'", "DestinationIp: 203.0.113.9")
    report = lint.lint_rule(text)
    assert report["passed"] is False
    assert (
        _checks(report)["Not tied to one host, address or account"]["passed"] is False
    )
    assert report["findings"][0]["value"] == "203.0.113.9"


def _merge_bomb(depth):
    lines = ["a0: &a0 {x: 1}"]
    for n in range(1, depth):
        lines.append(f"a{n}: &a{n} {{<<: [*a{n - 1}, *a{n - 1}]}}")
    return "\n".join(lines) + "\n"


@pytest.mark.parametrize(
    "text",
    [
        _merge_bomb(14),
        RULE + "copy: &one {a: 1}\nagain: *one\n",
        RULE + "merged: {<<: {a: 1}}\n",
        "[" * 50_000,
        "a:\n" + "".join(" " * i + "b:\n" for i in range(1, 2000)),
    ],
    ids=["merge bomb", "alias", "merge key", "deep flow", "deep block"],
)
def test_yaml_that_multiplies_or_nests_without_bound_is_refused_before_it_is_loaded(
    text,
):
    assert sigma_text.unsafe_because(text)
    assert sigma_text.parse(text) is None
    assert sigma_text.fill_in(text, None) == (text, [])
    report = lint.lint_rule(text)
    assert report["passed"] is False
    assert report["checks"][0]["name"] == "Written out in full"


def test_a_rule_with_a_literal_merge_like_value_or_normal_depth_still_parses():
    text = RULE + "note: 'keep <<: this and &that *there'\n"
    assert sigma_text.unsafe_because(text) is None
    assert sigma_text.parse(text)["note"].startswith("keep")


def test_a_filled_in_or_tuned_rule_stays_free_of_aliases():
    text, _ = sigma_text.fill_in(RULE, "T1059.001")
    tuned = sigma_text.add_exclusion(
        text, sigma_text.Exclusion("User", "contains", ("svc",))
    )
    assert sigma_text.unsafe_because(tuned) is None


def test_structure_is_blocking_and_advice_is_not():
    assert lint.lint_rule("title: only a title\n")["passed"] is False
    bare = "title: x\nlogsource: {product: windows}\ndetection: {a: {b: c}, condition: a}\n"
    report = lint.lint_rule(bare)
    assert report["passed"] is True
    assert _checks(report)["Tagged with an ATT&CK technique"]["passed"] is False


def test_an_exclusion_narrows_every_condition_and_replays_as_a_person_expects(
    attack_row, signed_macro_row
):
    exclusion = sigma_text.Exclusion(
        "ParentCommandLine", "contains", ("trusted-macros",)
    )
    tuned = sigma_text.add_exclusion(RULE, exclusion)
    assert replay_sigma(RULE, [attack_row], [signed_macro_row])["ruled_out_matched"]
    result = replay_sigma(tuned, [attack_row], [signed_macro_row])
    assert result["matched"] is True and result["ruled_out_matched"] is False

    again = sigma_text.add_exclusion(
        tuned, sigma_text.Exclusion("Image", "endswith", ("\\calc.exe",))
    )
    detection = sigma_text.parse(again)["detection"]
    assert {"filter_person_1", "filter_person_2"} <= set(detection)
    assert detection["condition"].count("not filter_person") == 2


@pytest.mark.parametrize(
    "field,match,values",
    [("a|re", "contains", ("x",)), ("a", "regex", ("x",)), ("a", "contains", ())],
)
def test_a_bad_exclusion_is_refused(field, match, values):
    with pytest.raises(sigma_text.NotARule):
        sigma_text.Exclusion(field, match, values)


def test_an_exclusion_needs_a_rule_it_can_change():
    with pytest.raises(sigma_text.NotARule):
        sigma_text.add_exclusion(
            "title: x\n", sigma_text.Exclusion("a", "contains", ("b",))
        )
