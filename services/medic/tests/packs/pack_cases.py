"""Rule test cases for the bundled medic-core pack (F7), run by the engine's vector runner.

Each rule `rules/<file>.yaml` in the pack source has a case file
`medic_core/<class>/<file>.yaml` here: `{rule: <id>, cases: [...]}`. A case is an
E3 vector (semantics.md §8) minus `rules`, plus:
- `fires`: whether the rule must open an incident (≥ 1 true and ≥ 1 false case);
- `also`: other pack rules to run beside it (for suppression cases);
- `suppression: pack`: use the pack's suppression.yaml, limited to the rules present.

Log lines earn `template_trust: catalog` only if the pack's catalog.yaml lists their
(logger, template), so a rule whose template is missing from the catalog fails its
firing case here. The test source lives outside the pack because pack_build refuses
any file it doesn't ship (F1).
"""

from __future__ import annotations

from functools import cache
from pathlib import Path

import yaml

from services.medic.contracts.rule_check import _StrictLoader

MEDIC = Path(__file__).resolve().parents[2]
PACK = MEDIC / "packs" / "medic-core"
CASES = Path(__file__).resolve().parent / "medic_core"
META = {"rule", "fires", "also", "suppression", "why"}


def load_yaml(path: Path):
    """Pack files parse with the loader's strict YAML (no aliases or duplicate keys)."""
    return yaml.load(path.read_text(encoding="utf-8"), Loader=_StrictLoader)


@cache
def pack_rules() -> dict[str, dict]:
    """Rule id -> rule, for every rule file in the pack source."""
    rules = (load_yaml(p) for p in sorted((PACK / "rules").glob("*.yaml")))
    return {r["id"]: r for r in rules}


def catalog() -> set[tuple[str, str]]:
    return {
        (e["logger"], e["template"])
        for e in load_yaml(PACK / "catalog.yaml")["entries"]
    }


def suppression() -> list[dict]:
    """The pack's entries in the engine's shape (the engine takes no `why`)."""
    entries = load_yaml(PACK / "suppression.yaml")["entries"]
    return [{k: v for k, v in e.items() if k != "why"} for e in entries]


def case_files() -> list[Path]:
    return sorted(CASES.glob("*/*.yaml"))


def case_file(rule: dict) -> Path:
    return CASES / rule["fault"]["class"] / f"{rule['id'].replace('.', '-')}.yaml"


def cases(rule_id: str) -> list[dict]:
    rule = pack_rules()[rule_id]
    doc = load_yaml(case_file(rule))
    assert doc["rule"] == rule_id, f"{case_file(rule)} names {doc['rule']}"
    return doc["cases"]


def _present(sel: dict, ids: set[str]) -> bool:
    return "rule" not in sel or sel["rule"] in ids


def vector(rule_id: str, case: dict) -> dict:
    """The case as a runnable E3 vector: rules inline, trust from the catalog."""
    rules = pack_rules()
    ids = [rule_id, *case.get("also", [])]
    trusted = catalog()
    vec = {k: v for k, v in case.items() if k not in META}
    vec["title"] = case.get("why", case["id"])
    vec["covers"] = ["§2"]
    vec["rules"] = [{"inline": rules[i]} for i in ids]
    vec["logs"] = [
        {**line, "trusted": (line["logger"], line["template"]) in trusted}
        for line in case.get("logs", [])
    ]
    if case.get("suppression") == "pack":
        vec["suppression"] = [
            e
            for e in suppression()
            if _present(e["parent"], set(ids)) and _present(e["children"], set(ids))
        ]
    return vec
