"""G2 contract: decision record schema v1 and the hash chain (K1 T-04, T-17; C4 6.3, 6.5)."""

from __future__ import annotations

import json
import sys
from copy import deepcopy
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))
from decision_chain import (
    GENESIS,
    TICK_S,
    ChainError,
    canonical,
    incident_id,
    record_hash,
    seal,
    verify,
)

SCHEMA = json.loads((ROOT / "decision-record.schema.json").read_text())
VALIDATOR = Draft202012Validator(SCHEMA)
FIX = ROOT / "fixtures" / "decision-records"
CHAINS = sorted((FIX / "valid").glob("*.jsonl"))
INVALID = sorted((FIX / "invalid").glob("*.json"))
ALLOWED_TYPES = {
    "enum",
    "number",
    "id",
    "timestamp",
    "hash",
    "redacted_excerpt",
    "pack_text",
}
BODY_TYPES = [
    "incident_opened",
    "incident_updated",
    "incident_resolved",
    "feedback",
    "adjudication",
    "anchor",
    "store_reset",
    "pack_event",
    "gap",
]
ENGINE_TYPES = {"incident_opened", "incident_updated", "incident_resolved"}


def load(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines()]


def pilot_day() -> list[dict]:
    return load(FIX / "valid" / "chain-01-pilot-day.jsonl")


# --- schema -------------------------------------------------------------------


def test_schema_is_valid_2020_12() -> None:
    Draft202012Validator.check_schema(SCHEMA)


@pytest.mark.parametrize("path", CHAINS, ids=lambda p: p.stem)
def test_every_record_in_valid_chains_validates(path: Path) -> None:
    for rec in load(path):
        errors = [e.message for e in VALIDATOR.iter_errors(rec)]
        assert errors == [], (rec["seq"], errors)


@pytest.mark.parametrize("path", INVALID, ids=lambda p: p.stem)
def test_invalid_fixture_is_refused(path: Path) -> None:
    doc = json.loads(path.read_text())
    reason = doc.pop("$comment")
    assert not VALIDATOR.is_valid(doc), reason


def test_valid_chains_cover_every_record_type() -> None:
    seen = {rec["type"] for path in CHAINS for rec in load(path)}
    assert seen == set(BODY_TYPES)


# --- K1 T-04: every stored field is typed -----------------------------------------


def _resolve(node: dict) -> dict:
    ref = node.get("$ref")
    return SCHEMA["$defs"][ref.split("/")[-1]] if ref else node


def _leaves(node: dict, path: str):
    node = _resolve(node)
    if "x-medic-type" in node:
        yield path, node["x-medic-type"]
        return
    walked = False
    for key, sub in node.get("properties", {}).items():
        walked = True
        yield from _leaves(sub, f"{path}.{key}")
    if "items" in node:
        walked = True
        yield from _leaves(node["items"], f"{path}[]")
    for i, branch in enumerate(node.get("oneOf", []) + node.get("anyOf", [])):
        walked = True
        yield from _leaves(branch, f"{path}|{i}")
    if not walked and node.get("type") != "object":
        yield path, None


def test_every_leaf_field_carries_an_allowed_type_tag() -> None:
    roots = [("record", {k: v for k, v in SCHEMA.items() if k != "$defs"})]
    roots += [(name, SCHEMA["$defs"][name]) for name in BODY_TYPES]
    leaves = [leaf for name, node in roots for leaf in _leaves(node, name)]
    untagged = [p for p, t in leaves if t is None]
    unknown = [(p, t) for p, t in leaves if t is not None and t not in ALLOWED_TYPES]
    assert untagged == [], f"untagged stored fields: {untagged}"
    assert unknown == [], f"tags outside K1's list: {unknown}"
    assert len(leaves) > 60


def test_free_text_is_only_ever_excerpt_or_pack_text() -> None:
    def strings(node, path=""):
        node = _resolve(node)
        if node.get("type") == "string" and "pattern" not in node:
            yield path, node.get("x-medic-type"), node.get("maxLength")
        for k, sub in node.get("properties", {}).items():
            yield from strings(sub, f"{path}.{k}")
        if "items" in node:
            yield from strings(node["items"], f"{path}[]")
        for b in node.get("oneOf", []) + node.get("anyOf", []):
            yield from strings(b, path)

    found = [s for name in BODY_TYPES for s in strings(SCHEMA["$defs"][name], name)]
    assert found, "expected excerpts and advice"
    for path, tag, max_len in found:
        assert tag in {"redacted_excerpt", "pack_text"}, path
        assert max_len is not None and max_len <= 1000, path


# --- size (C4 6.4 assumed <= 8 KB per decision) -------------------------------------


def test_worst_case_record_size() -> None:
    rec = deepcopy(pilot_day()[0])
    body = rec["body"]
    ev = deepcopy(body["evidence"][0])
    ev["excerpt"]["text"] = "x" * 500
    body["evidence"] = [ev] * 10
    body["advice"] = {"summary": "s" * 200, "fix": "f" * 1000}
    body["group"] = [{"name": "n" * 32, "value": "v" * 128}] * 3
    rec = seal({k: v for k, v in rec.items() if k not in ("seq", "prev", "hash")}, None)
    assert VALIDATOR.is_valid(rec)
    size = len(canonical(rec))
    # ASCII worst case is ~9.3 KB, above C4's 8 KB assumption (reported to C4). Non-ASCII
    # excerpts can be larger, so the writer (G3) also enforces RECORD_CAP_BYTES (doc section 3).
    assert size <= 9_800, size


# --- K1 T-17: append-only, hash-chained ----------------------------------------------


@pytest.mark.parametrize("path", CHAINS, ids=lambda p: p.stem)
def test_valid_chains_verify(path: Path) -> None:
    records = load(path)
    assert verify(records) == records[-1]["hash"]


def test_hash_ignores_key_order_and_whitespace() -> None:
    rec = pilot_day()[0]
    shuffled = json.loads(json.dumps(dict(reversed(list(rec.items())))))
    assert record_hash(shuffled) == rec["hash"]


def test_first_record_links_to_genesis() -> None:
    first = pilot_day()[0]
    assert first["seq"] == 0 and first["prev"] == GENESIS


def _code(records: list[dict]) -> str:
    with pytest.raises(ChainError) as err:
        verify(records)
    return err.value.code


def test_editing_a_past_record_breaks_the_chain() -> None:
    records = pilot_day()
    records[1]["body"]["value"] = "false_alarm"  # flip an admin's agree
    assert _code(records) == "E-HASH"


def test_editing_and_rehashing_one_record_still_breaks_the_next_link() -> None:
    records = pilot_day()
    records[1]["body"]["value"] = "false_alarm"
    records[1]["hash"] = record_hash(records[1])
    assert _code(records) == "E-LINK"


def test_deleting_a_middle_record_is_detected() -> None:
    records = pilot_day()
    del records[3]
    assert _code(records) == "E-SEQ"


def test_reordering_is_detected() -> None:
    records = pilot_day()
    records[2], records[3] = records[3], records[2]
    assert _code(records) == "E-SEQ"


def test_purge_without_an_anchor_is_detected() -> None:
    records = load(FIX / "valid" / "chain-02-after-purge.jsonl")
    assert records[-1]["type"] == "anchor"
    assert _code(records[:-1]) == "E-UNANCHORED"


def test_anchor_must_carry_the_last_deleted_hash() -> None:
    records = load(FIX / "valid" / "chain-02-after-purge.jsonl")
    records[-1]["body"]["last_deleted_hash"] = "f" * 64
    records[-1]["hash"] = record_hash(records[-1])
    assert _code(records) == "E-UNANCHORED"


def test_store_reset_must_name_the_old_head() -> None:
    records = load(FIX / "valid" / "chain-03-store-reset.jsonl")
    records[0]["body"]["previous_head"]["hash"] = "e" * 64
    records[0]["hash"] = record_hash(records[0])
    assert _code(records) == "E-UNANCHORED"


# --- A3-5 (decided 2026-10-07): deterministic decisions, so replay is byte-identical ---


def _on_tick(ts: str) -> bool:
    if "." in ts:
        return False
    h, m, s = (int(x) for x in ts[11:19].split(":"))
    return (h * 3600 + m * 60 + s) % TICK_S == 0


@pytest.mark.parametrize("path", CHAINS, ids=lambda p: p.stem)
def test_engine_records_are_stamped_with_their_tick(path: Path) -> None:
    for rec in load(path):
        if rec["type"] in ENGINE_TYPES:
            assert _on_tick(rec["at"]), (rec["seq"], rec["at"])
        if rec["type"] == "incident_opened":
            assert _on_tick(rec["body"]["active_since"]), rec["seq"]


@pytest.mark.parametrize("path", CHAINS, ids=lambda p: p.stem)
def test_incident_ids_are_derived_not_random(path: Path) -> None:
    for rec in load(path):
        if rec["type"] == "incident_opened":
            b = rec["body"]
            rule = b["rule"]["id"] if b["rule"] else None
            want = incident_id(
                b["instance_id"], rule, b.get("group", []), b["active_since"]
            )
            assert b["incident_id"] == want, rec["seq"]


def test_incident_id_depends_on_every_input_and_not_on_group_order() -> None:
    g = [{"name": "a", "value": "1"}, {"name": "b", "value": "2"}]
    base = incident_id("mi_0123456789abcdef", "ingest.x-y", g, "2026-12-07T09:13:00Z")
    assert base == incident_id(
        "mi_0123456789abcdef", "ingest.x-y", list(reversed(g)), "2026-12-07T09:13:00Z"
    )
    for other in (
        incident_id("mi_fedcba9876543210", "ingest.x-y", g, "2026-12-07T09:13:00Z"),
        incident_id("mi_0123456789abcdef", "ingest.x-z", g, "2026-12-07T09:13:00Z"),
        incident_id("mi_0123456789abcdef", "ingest.x-y", g[:1], "2026-12-07T09:13:00Z"),
        incident_id("mi_0123456789abcdef", "ingest.x-y", g, "2026-12-07T09:13:15Z"),
        incident_id("mi_0123456789abcdef", None, g, "2026-12-07T09:13:00Z"),
    ):
        assert other != base
    assert Draft202012Validator(SCHEMA["$defs"]["incident_id"]).is_valid(base)


# --- X1 ⚑5a (decided 2026-10-07): instance_id is random, persisted, kept across resets ---


@pytest.mark.parametrize("path", CHAINS, ids=lambda p: p.stem)
def test_one_store_has_one_instance_id(path: Path) -> None:
    ids = {
        rec["body"]["instance_id"]
        for rec in load(path)
        if rec["type"] in ("incident_opened", "store_reset")
    }
    assert len(ids) == 1, ids


def test_instance_id_is_random_shaped_never_a_hostname() -> None:
    node = Draft202012Validator(SCHEMA["$defs"]["instance_id"])
    assert node.is_valid("mi_3f9a2c1b0d4e5f60")
    for bad in ("partner-a-prod", "vigil-0.vigil.svc.cluster.local", "mi_XYZ"):
        assert not node.is_valid(bad), bad


# --- X1 ⚑3a (decided 2026-10-07): pack lifecycle is in the chain ---------------------


def test_valid_chains_cover_every_pack_event() -> None:
    seen = {
        rec["body"]["event"]
        for path in CHAINS
        for rec in load(path)
        if rec["type"] == "pack_event"
    }
    assert seen == {"imported", "reverted", "override", "rule_skipped"}


# --- S0 review fixes ------------------------------------------------------------------


def test_engine_timestamps_carry_no_fraction() -> None:
    # R5: active_since feeds the incident id, so 09:13:00Z and 09:13:00.000Z must not both be legal.
    rec = deepcopy(next(r for r in pilot_day() if r["type"] == "incident_opened"))
    for field, where in (("at", rec), ("active_since", rec["body"])):
        doc = deepcopy(rec)
        target = doc if where is rec else doc["body"]
        target[field] = target[field].replace("Z", ".000Z")
        assert not VALIDATOR.is_valid(doc), field


def test_non_engine_records_may_carry_wall_clock_fractions() -> None:
    rec = deepcopy(next(r for r in pilot_day() if r["type"] == "feedback"))
    rec["at"] = "2026-12-07T09:40:00.123456Z"
    assert VALIDATOR.is_valid(rec)


@pytest.mark.parametrize("path", CHAINS, ids=lambda p: p.stem)
def test_evidence_is_ordered_by_time_then_observation_id(path: Path) -> None:
    # R6: two correct engines must write the same bytes.
    for rec in load(path):
        ev = rec["body"].get("evidence") if rec["type"] == "incident_opened" else None
        if ev:
            from datetime import datetime

            def instant(ts: str) -> datetime:
                return datetime.fromisoformat(ts)

            # N3: compare instants, not strings ("…:00Z" vs "…:00.5Z").
            keys = [(instant(e["t"]), e["observation_id"]) for e in ev]
            assert keys == sorted(keys), rec["seq"]


def test_sensor_blind_incidents_are_grouped_by_sensor() -> None:
    # R8: one group per sensor (E3 §2), so two blind sensors get two incident ids.
    for rec in pilot_day():
        if rec["type"] == "incident_opened" and rec["body"]["subject"] == "watcher":
            assert [g["name"] for g in rec["body"]["group"]] == ["sensor"]


def test_detection_types_in_fixtures_match_their_rules() -> None:
    # R7: absence rules declare it (ENGINE_API §2); the record copies the declaration.
    want = {
        "pipeline.triage-stalled": "absence",
        "pipeline.agent-worker-down": "event",
        "ingest.integration-config-incomplete": "event",
        "watcher.sensor-blind": "event",
    }
    for rec in pilot_day():
        if rec["type"] == "incident_opened":
            b = rec["body"]
            assert b["detection_type"] == want[b["rule"]["id"]], b["rule"]["id"]


P010 = {"id": "medic-core", "version": "0.1.0"}


def _pack_record(body: dict) -> dict:
    return {
        "v": 1,
        "seq": 0,
        "at": "2026-12-10T16:00:00Z",
        "type": "pack_event",
        "prev": GENESIS,
        "hash": GENESIS,
        "body": body,
    }


@pytest.mark.parametrize(
    "body",
    [
        {"event": "reverted", "pack": P010, "previous": None, "by": "user:1"},
        {"event": "override", "pack": P010, "previous": None, "by": "user:1"},
        {
            "event": "imported",
            "pack": P010,
            "previous": None,
            "source": "bundled",
            "rule": "ingest.a-b",
        },
        {
            "event": "rule_skipped",
            "pack": P010,
            "rule": "ingest.a-b",
            "reason": "needs_engine_minor",
            "by": "user:1",
        },
        {
            "event": "reverted",
            "pack": P010,
            "previous": P010,
            "by": "user:1",
            "source": "bundled",
        },
    ],
)
def test_pack_events_carry_only_their_own_fields(body: dict) -> None:
    # R11: a revert or override always names what it replaced; no event borrows another's fields.
    assert VALIDATOR.is_valid(
        _pack_record({"event": "reverted", "pack": P010, "previous": P010, "by": "u"})
    )
    assert not VALIDATOR.is_valid(_pack_record(body))


def test_purge_anchor_spans_the_deleted_records_times() -> None:
    # R13: `at` isn't monotonic across types, so the anchor's range is min..max of what it deleted.
    day = pilot_day()
    purged = load(FIX / "valid" / "chain-02-after-purge.jsonl")
    anchor = purged[-1]["body"]
    deleted = day[anchor["deleted_first_seq"] : anchor["deleted_last_seq"] + 1]
    assert anchor["deleted_from"] == min(r["at"] for r in deleted)
    assert anchor["deleted_to"] == max(r["at"] for r in deleted)


def test_replay_feedback_names_the_replay_it_judged() -> None:
    # S0 review R12 (decided 2026-10-07): replay ids equal live ids (A3-5), so feedback from a
    # replay session must say which replay store it was looking at.
    rec = deepcopy(next(r for r in pilot_day() if r["body"].get("source") == "replay"))
    assert VALIDATOR.is_valid(rec)
    rec["body"].pop("replay_head")
    assert not VALIDATOR.is_valid(rec)
    live = deepcopy(next(r for r in pilot_day() if r["body"].get("source") == "live"))
    live["body"]["replay_head"] = "a" * 64
    assert not VALIDATOR.is_valid(live)


READS = {  # the signals each fixture rule reads (fixtures/rules/valid, E3 built-ins)
    "ingest.integration-config-incomplete": {"log_soc_daemon"},
    "pipeline.agent-worker-down": {"agent_worker_readyz", "pods"},
    "pipeline.triage-stalled": {"daemon_status.processor", "triage"},
    "watcher.sensor-blind": {"sensor_health"},
}


@pytest.mark.parametrize("path", CHAINS, ids=lambda p: p.stem)
def test_evidence_comes_only_from_signals_the_rule_reads(path: Path) -> None:
    # N5: evidence selection is per signal the rule reads (decision-record.md §3).
    for rec in load(path):
        if rec["type"] == "incident_opened":
            b = rec["body"]
            assert {e["signal"] for e in b["evidence"]} <= READS[b["rule"]["id"]], rec[
                "seq"
            ]


@pytest.mark.parametrize("path", CHAINS, ids=lambda p: p.stem)
def test_incident_opened_always_writes_its_group(path: Path) -> None:
    # One spelling for "no group": [] (it feeds the incident id).
    for rec in load(path):
        if rec["type"] == "incident_opened":
            assert "group" in rec["body"], rec["seq"]


def test_a_bundled_import_has_no_admin() -> None:
    body = {
        "event": "imported",
        "pack": P010,
        "previous": None,
        "source": "bundled",
        "by": "u",
    }
    assert not VALIDATOR.is_valid(_pack_record(body))


# --- S2-5 (decided 2026-10-07): a time Medic wasn't watching is in the chain ----------


def _gap_chain() -> list[dict]:
    return load(FIX / "valid" / "chain-04-gap.jsonl")


def test_gap_chain_records_a_stall_and_a_time_off() -> None:
    gaps = [r["body"] for r in _gap_chain() if r["type"] == "gap"]
    assert [g["reason"] for g in gaps] == ["stalled", "off"]
    for g in gaps:
        assert g["from"] <= g["to"]


def test_a_gap_is_written_after_the_time_it_covers() -> None:
    # C4 §6.8: written at start-up, so the wall-clock `at` is never before `to`.
    for rec in _gap_chain():
        if rec["type"] == "gap":
            assert rec["at"] >= rec["body"]["to"], rec["seq"]


def test_a_gap_that_ends_before_it_starts_breaks_the_chain() -> None:
    records = _gap_chain()
    i = next(i for i, r in enumerate(records) if r["type"] == "gap")
    body = records[i]["body"]
    body["from"], body["to"] = body["to"], body["from"]
    for j in range(i, len(records)):  # a forger re-seals everything after it
        records[j] = seal(
            {k: v for k, v in records[j].items() if k not in ("prev", "hash")},
            records[j - 1],
        )
    assert VALIDATOR.is_valid(records[i]), "the schema can't compare two fields"
    with pytest.raises(ChainError) as err:
        verify(records)
    assert (err.value.code, err.value.seq) == ("E-GAP", records[i]["seq"])


def test_gap_times_compare_as_instants_not_strings() -> None:
    # "…:00.5Z" sorts before "…:00Z" as a string but is the later instant.
    gap = {
        "v": 1,
        "at": "2026-12-07T10:00:01Z",
        "type": "gap",
        "body": {
            "from": "2026-12-07T10:00:00Z",
            "to": "2026-12-07T10:00:00.5Z",
            "reason": "off",
        },
    }
    records = [seal(gap, None)]
    assert VALIDATOR.is_valid(records[0])
    assert verify(records) == records[0]["hash"]


def test_a_gap_reason_is_stalled_or_off() -> None:
    rec = deepcopy(next(r for r in _gap_chain() if r["type"] == "gap"))
    for reason in ("stalled", "off"):
        rec["body"]["reason"] = reason
        assert VALIDATOR.is_valid(rec), reason
    rec["body"]["reason"] = "maintenance"
    assert not VALIDATOR.is_valid(rec)
