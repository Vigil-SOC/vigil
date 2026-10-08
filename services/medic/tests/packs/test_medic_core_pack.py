"""The whole medic-core pack source loads: rule_check per rule, then pack_build and
pack_check on the built file, unsigned (dev mode; F5/F2c sign it)."""

from __future__ import annotations

from datetime import datetime, timedelta

from services.medic.contracts import pack_build, pack_check, rule_check
from services.medic.engine import load_rule_file
from services.medic.tests.packs.pack_cases import MEDIC, PACK, load_yaml, pack_rules

VIGIL_VERSION = (MEDIC.parents[1] / "VERSION").read_text().strip()


def _source() -> dict:
    return load_yaml(PACK / "pack.yaml")


def _now() -> datetime:
    created = _source()["pack"]["created_at"]
    return datetime.fromisoformat(created) + timedelta(days=1)


def test_every_rule_passes_the_loader_and_declares_detection() -> None:
    paths = sorted((PACK / "rules").glob("*.yaml"))
    assert paths, "the pack has no rules"
    for path in paths:
        res = rule_check.check_text(path.read_text(encoding="utf-8"))
        assert res.ok, (path.name, res.errors)
        assert res.skipped is None, path.name
        rule = load_yaml(path)
        assert "detection" in rule, f"{path.name}: declare detection (F7 review)"
        assert load_rule_file(path).id == rule["id"]


def test_the_pack_builds_reproducibly_and_passes_pack_check() -> None:
    built = pack_build.build(PACK)
    assert built == pack_build.build(PACK)
    res = pack_check.check_pack(built, vigil_version=VIGIL_VERSION, now=_now())
    assert res.errors == []
    assert set(res.rules) == set(pack_rules())
    assert res.skipped_rules == []


def test_the_pack_is_the_dev_channel_build_of_medic_core() -> None:
    pack = _source()["pack"]
    assert (pack["id"], pack["channel"], pack["publisher"]) == (
        "medic-core",
        "dev",
        "deeptempo",
    )


def test_lane_1_rules_carry_no_runbook_yet() -> None:
    # G4's action vocabulary is still open (A2R-4): lane-1 rules ship runbook: none.
    assert not (PACK / "runbooks").exists() or not any((PACK / "runbooks").iterdir())
