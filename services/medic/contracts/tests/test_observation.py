"""D3 contract: observation schema v1 and fingerprint algorithm fp1."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))
from fingerprint_ref import SERVICES, LogLine, fingerprint, label_safe, normalize

SCHEMA = json.loads((ROOT / "observation.schema.json").read_text())
VALIDATOR = Draft202012Validator(SCHEMA)
OBS = ROOT / "fixtures" / "observations"
VALID = sorted((OBS / "valid").glob("*.json"))
INVALID = sorted((OBS / "invalid").glob("*.json"))
EXAMPLES = json.loads((ROOT / "fixtures" / "fingerprint" / "examples.json").read_text())
CATALOG = {tuple(c) for c in EXAMPLES["catalog"]}
D1_CLASSES = {"endpoint", "datastore", "log", "platform", "file", "host"}


def test_schema_is_valid_2020_12() -> None:
    Draft202012Validator.check_schema(SCHEMA)


@pytest.mark.parametrize("path", VALID, ids=lambda p: p.stem)
def test_valid_fixture_validates(path: Path) -> None:
    errors = list(VALIDATOR.iter_errors(json.loads(path.read_text())))
    assert errors == [], [e.message for e in errors]


@pytest.mark.parametrize("path", INVALID, ids=lambda p: p.stem)
def test_invalid_fixture_is_refused(path: Path) -> None:
    doc = json.loads(path.read_text())
    reason = doc.pop("$comment")  # so the only defect left is the intended one
    assert not VALIDATOR.is_valid(doc), reason


def test_one_valid_fixture_per_d1_class_plus_heartbeats() -> None:
    prefixes = {p.stem.split("-")[0] for p in VALID}
    assert D1_CLASSES <= prefixes
    assert "sensor" in prefixes


def test_log_fixtures_carry_the_reference_fingerprint() -> None:
    for path in VALID:
        doc = json.loads(path.read_text())
        if doc["kind"] != "log":
            continue
        log = doc["log"]
        assert log["fingerprint"].startswith("fp1_")
        assert (log["template_trust"] == "catalog") == (
            log["fingerprint_basis"] == "template"
        )


@pytest.mark.parametrize("group", EXAMPLES["same"], ids=lambda g: g["name"])
def test_lines_differing_only_in_variables_share_a_fingerprint(group: dict) -> None:
    catalog = CATALOG if group["use_catalog"] else set()
    fps = {fingerprint(LogLine(**line), catalog).fingerprint for line in group["lines"]}
    assert fps == {group["expected"]}  # also pins fp1 output: hashes are stable


@pytest.mark.parametrize("pair", EXAMPLES["differ"], ids=lambda p: p["name"])
def test_material_differences_change_the_fingerprint(pair: dict) -> None:
    fps = [fingerprint(LogLine(**line), CATALOG).fingerprint for line in pair["lines"]]
    assert fps == pair["expected"]
    assert fps[0] != fps[1]


def test_untrusted_line_never_takes_a_trusted_fingerprint() -> None:
    template = EXAMPLES["catalog"][0][1]
    logger = EXAMPLES["catalog"][0][0]
    trusted = fingerprint(
        LogLine(
            "soc-daemon", "python_json", "WARNING", logger, template, "E " + template
        ),
        CATALOG,
    )
    forged = fingerprint(
        LogLine("soc-daemon", "python_json", "WARNING", logger, template, template),
        CATALOG,
    )
    assert trusted.template_trust == "catalog"
    assert forged.template_trust == "untrusted"
    assert trusted.fingerprint != forged.fingerprint


def test_args_are_recovered_from_catalog_template() -> None:
    template = EXAMPLES["catalog"][0][1]
    fp = fingerprint(
        LogLine(
            "soc-daemon",
            "python_json",
            "WARNING",
            EXAMPLES["catalog"][0][0],
            template,
            "Azure Sentinel configuration incomplete (missing: client_secret); "
            "skipping polls until it is completed",
        ),
        CATALOG,
    )
    assert fp.args == ("Azure Sentinel", "client_secret")


@pytest.mark.parametrize(
    "text",
    ["a" * 50_000 + "!", "'" * 20_000, "/a" * 10_000, "1." * 10_000, "x@y." * 5_000],
)
def test_normalizer_is_bounded_on_hostile_input(text: str) -> None:
    import time

    start = time.perf_counter()
    out = normalize(text)
    assert time.perf_counter() - start < 1.0
    assert len(out) <= 160


# X1 #1 (decided 2026-10-07, ⚑1a): group values and labels follow D3's label rule at extraction.
LABEL = Draft202012Validator(SCHEMA["$defs"]["label_value"])


@pytest.mark.parametrize(
    "value", ["elastic", "splunk-prod", "a.b:c/d@e+f_g", "x" * 128]
)
def test_label_safe_values_pass_through_unchanged(value: str) -> None:
    assert label_safe(value) == value


@pytest.mark.parametrize(
    "value",
    ["Azure Sentinel", "elastic; DROP TABLE", "", "x" * 129, "Ünïcode", "E" * 200],
)
def test_other_values_become_h_plus_16_hex_of_sha256(value: str) -> None:
    import hashlib

    out = label_safe(value)
    assert out == "h_" + hashlib.sha256(value.encode()).hexdigest()[:16]
    assert LABEL.is_valid(out)


# S0 review R10: the engine's own markers can't be forged by a label-safe raw value.
@pytest.mark.parametrize("value", ["h_ff9cf5afca192342", "h_x", "__overflow__"])
def test_values_that_look_like_engine_markers_are_hashed(value: str) -> None:
    import hashlib

    assert label_safe(value) == "h_" + hashlib.sha256(value.encode()).hexdigest()[:16]


def test_hashed_values_never_equal_a_forged_marker() -> None:
    real = label_safe("Azure Sentinel")
    assert label_safe(real) != real  # applied exactly once, at extraction


# X1 #6 (D3): fp1 hashes the service name, so it comes from one closed,
# shape-independent list. Helm's "<fullname>-soc-daemon" is reported as "soc-daemon".
def test_service_is_a_closed_list_shared_with_the_reference_code() -> None:
    assert SCHEMA["$defs"]["service"]["enum"] == list(SERVICES)
    target = SCHEMA["properties"]["target"]["properties"]["service"]
    assert target == {"$ref": "#/$defs/service"}


def test_fingerprint_refuses_an_unlisted_service() -> None:
    with pytest.raises(ValueError):
        fingerprint(
            LogLine(service="vigil-soc-daemon", format="text", raw="ERROR: x"), set()
        )


def test_runbook_targets_are_listed_services() -> None:
    manifest = json.loads((ROOT / "pack-manifest.schema.json").read_text())
    targets = manifest["$defs"]["runbook"]["properties"]["action"]["properties"][
        "target"
    ]["enum"]
    assert set(targets) <= set(SERVICES)
