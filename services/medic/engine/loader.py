"""Dev-mode rule loading: one rule file or dict through the contract's rule_check.

No pack loader here (F6 owns packs, signatures and last-known-good). Every
rule_check error surfaces unchanged, code and message, in RuleLoadError.errors.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from pathlib import Path

from services.medic.contracts import rule_check

DEV_PACK = ("medic-dev", "0.0.0")  # records name this pack for dev-mode rules
DEFAULT_FOR_S, DEFAULT_KEEP_S = 120, 300  # semantics.md §4


class RuleLoadError(ValueError):
    def __init__(self, errors: list[tuple[str, str]]) -> None:
        self.errors = list(errors)
        super().__init__("; ".join(f"{code}: {msg}" for code, msg in self.errors))

    @property
    def codes(self) -> set[str]:
        return {code for code, _ in self.errors}


@dataclass(frozen=True)
class LoadedRule:
    rule: dict
    input_trust: str  # derived by rule_check (ENGINE_API.md §5)
    detection: str
    skipped: str | None  # set when the rule needs a newer engine minor
    params: dict[str, float] = field(default_factory=dict)  # durations in seconds
    pack: tuple[str, str] = DEV_PACK

    @property
    def id(self) -> str:
        return self.rule["id"]

    def param(self, name: str) -> float:
        return self.params[name]

    def seconds(self, key: str, default: int) -> int:
        return rule_check.seconds(self.rule[key]) if key in self.rule else default


def load_rule(
    rule: dict, *, params: dict | None = None, pack: tuple[str, str] = DEV_PACK
) -> LoadedRule:
    res = rule_check.check(rule)
    if not res.ok:
        raise RuleLoadError(res.errors)
    return LoadedRule(
        rule=copy.deepcopy(rule),
        input_trust=res.input_trust,
        detection=res.detection,
        skipped=res.skipped,
        params=_resolve_params(rule.get("params", {}), params or {}),
        pack=pack,
    )


def load_rule_file(path: Path | str, *, params: dict | None = None) -> LoadedRule:
    raw = Path(path).read_bytes()[: rule_check.MAX_RULE_BYTES + 1]
    doc, res = rule_check.load(raw.decode("utf-8", errors="replace"))
    if doc is None:
        raise RuleLoadError(res.errors)
    return load_rule(doc, params=params)


def _resolve_params(specs: dict, overrides: dict) -> dict[str, float]:
    """Site overrides (≤ 8 params) must name a declared param and stay in [min, max]."""
    errors = [("E-PARAM", f"no param named {n}") for n in overrides if n not in specs]
    out: dict[str, float] = {}
    for name, spec in specs.items():
        value = overrides.get(name, spec["default"])
        try:
            v, lo, hi = (
                rule_check.seconds(x) if spec["type"] == "duration" else float(x)
                for x in (value, spec["min"], spec["max"])
            )
        except (KeyError, TypeError, ValueError, IndexError):
            errors.append(("E-PARAM", f"{name}: {value!r} is not a {spec['type']}"))
            continue
        if not lo <= v <= hi:
            errors.append(("E-PARAM", f"{name}: {value!r} outside [min, max]"))
        out[name] = v
    if errors:
        raise RuleLoadError(errors)
    return out
