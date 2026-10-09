"""F1 contract: content-pack format medic.pack/v1."""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml
from jsonschema import Draft202012Validator

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))
from pack_build import build
from pack_check import MAX_PACK_BYTES, Lineage, check_pack

PACKS = ROOT / "fixtures" / "packs"
SOURCE = PACKS / "sample-0.1.0"
BUILT = PACKS / "sample-0.1.0.medicpack.json"
EXPECTED = json.loads((PACKS / "invalid" / "expected.json").read_text())
NOW = datetime(2026, 10, 7, 12, tzinfo=UTC)
OK = {"vigil_version": "0.6.0", "now": NOW}


def test_schema_is_valid_2020_12() -> None:
    Draft202012Validator.check_schema(
        json.loads((ROOT / "pack-manifest.schema.json").read_text())
    )


def test_sample_pack_is_accepted() -> None:
    res = check_pack(BUILT.read_bytes(), **OK)
    assert res.ok, res.errors
    assert len(res.rules) == 12 and res.skipped_rules == []
    assert res.manifest["counts"] == {
        "rules": 12,
        "runbooks": 1,
        "suppressions": 4,
        "catalog_entries": 16,
    }


def test_build_is_reproducible_byte_for_byte() -> None:
    assert build(SOURCE) == BUILT.read_bytes()
    assert build(SOURCE) == build(SOURCE)


def test_sample_covers_every_pack_item_and_all_three_fault_classes() -> None:
    doc = json.loads(BUILT.read_text())
    kinds = {c["kind"] for c in doc["manifest"]["contents"]}
    assert kinds == {"rule", "runbook", "suppression", "catalog"}
    classes = {
        yaml.safe_load(t)["fault"]["class"]
        for p, t in doc["files"].items()
        if p.startswith("rules/")
    }
    assert classes == {"ingest", "llm", "pipeline"}
    lanes = {
        yaml.safe_load(t)["lane"]
        for p, t in doc["files"].items()
        if p.startswith("rules/")
    }
    assert lanes == {1, 2, 3}


@pytest.mark.parametrize("name", sorted(EXPECTED), ids=str)
def test_invalid_pack_is_refused_with_its_codes(name: str) -> None:
    data = (PACKS / "invalid" / f"{name}.medicpack.json").read_bytes()
    assert check_pack(data, **OK).codes == set(EXPECTED[name]["codes"]), EXPECTED[name][
        "why"
    ]


def test_every_invalid_file_has_an_expectation() -> None:
    files = {
        p.name.removesuffix(".medicpack.json")
        for p in (PACKS / "invalid").glob("*.medicpack.json")
    }
    assert files == set(EXPECTED)


def test_oversized_pack_is_refused_before_parsing() -> None:
    assert check_pack(b"{" * (MAX_PACK_BYTES + 1), **OK).codes == {"P-SIZE"}


@pytest.mark.parametrize(
    ("lkg", "override", "codes"),
    [
        (Lineage("medic-core", "dev", "0.0.9"), False, set()),
        (Lineage("medic-core", "dev", "0.1.0"), False, {"P-DOWNGRADE"}),
        (Lineage("medic-core", "dev", "0.2.0"), False, {"P-DOWNGRADE"}),
        (Lineage("medic-core", "dev", "0.2.0"), True, set()),
        (Lineage("medic-core", "stable", "0.0.1"), False, {"P-LINEAGE"}),
        (Lineage("other-pack", "dev", "0.0.1"), False, {"P-LINEAGE"}),
    ],
    ids=[
        "newer",
        "same-version",
        "older",
        "older-with-logged-override",
        "channel-switch",
        "other-pack",
    ],
)
def test_versions_are_monotonic_per_pack_and_channel(
    lkg: Lineage, override: bool, codes: set
) -> None:
    res = check_pack(
        BUILT.read_bytes(), **OK, last_known_good=lkg, override_lineage=override
    )
    assert res.codes == codes


@pytest.mark.parametrize(
    ("vigil", "ok"),
    [("0.6.0", True), ("0.7.9", True), ("0.8.0", False), ("0.5.9", False)],
)
def test_vigil_range_is_half_open(vigil: str, ok: bool) -> None:
    assert check_pack(BUILT.read_bytes(), vigil_version=vigil, now=NOW).ok is ok


def test_expiry_is_checked_at_import_time() -> None:
    assert check_pack(
        BUILT.read_bytes(), vigil_version="0.6.0", now=datetime(2027, 4, 5, tzinfo=UTC)
    ).codes == {"P-EXPIRED"}


def test_rule_needing_newer_engine_minor_is_skipped_not_refused(tmp_path: Path) -> None:
    src = tmp_path / "src"
    for p in SOURCE.rglob("*"):
        if p.is_file():
            dest = src / p.relative_to(SOURCE)
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(p.read_text())
    rule = src / "rules" / "llm-gateway-flapping.yaml"
    rule.write_text(
        rule.read_text().replace("revision: 1\n", 'revision: 1\nengine_api: "1.3"\n')
    )
    manifest = src / "pack.yaml"
    manifest.write_text(
        manifest.read_text().replace('engine_api: "1.0"', 'engine_api: "1.3"')
    )
    res = check_pack(build(src), **OK)
    assert res.ok, res.errors
    assert res.skipped_rules == ["llm.gateway-flapping"]


# S0 review R9: widening the catalog logger charset (X1 #14) must not let any spelling
# of the client-written frontend logger be catalogued (fingerprint.md §7, K1 T-05).
def _catalog_logger_validator():
    from jsonschema import Draft202012Validator

    schema = json.loads((ROOT / "pack-manifest.schema.json").read_text())
    node = schema["$defs"]["catalog"]["properties"]["entries"]["items"]
    return Draft202012Validator(node["properties"]["logger"])


@pytest.mark.parametrize(
    "logger",
    [
        "frontend",
        "frontend.app",
        "frontend:app",
        "frontend-app",
        "Frontend.app",
        "FRONTEND",
    ],
)
def test_no_spelling_of_the_frontend_logger_can_be_catalogued(logger: str) -> None:
    assert not _catalog_logger_validator().is_valid(logger)


@pytest.mark.parametrize(
    "logger",
    ["services.daemon.processor", "agent:worker", "agent-serve", "frontendish"],
)
def test_ordinary_loggers_can_be_catalogued(logger: str) -> None:
    assert _catalog_logger_validator().is_valid(logger)
