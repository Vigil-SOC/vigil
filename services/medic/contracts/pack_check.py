"""Reference import checks for Medic content packs (medic.pack/v1).

The executable form of F1 §4. The pack loader (F6) must accept and refuse exactly
what this does, with the same codes. It runs AFTER the signature (F2) has been
verified on the raw bytes, and before anything else touches the pack.
Order: size → strict JSON → schema → files and hashes → identity (dates, Vigil
range, lineage) → rules → suppression → catalog → runbooks → counts.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import re2
import yaml
from jsonschema import Draft202012Validator

if __package__:  # inside the services.medic package (A3 F-16)
    from .rule_check import ENGINE_API, _StrictLoader, check_text
else:  # run from the contracts folder (tests, CLI)
    from rule_check import ENGINE_API, _StrictLoader, check_text

HERE = Path(__file__).parent
_SCHEMA = json.loads((HERE / "pack-manifest.schema.json").read_text())


def _sub(ref: str) -> Draft202012Validator:
    return Draft202012Validator(
        {"$schema": _SCHEMA["$schema"], "$defs": _SCHEMA["$defs"], "$ref": ref}
    )


PACK = Draft202012Validator(_SCHEMA)
SUPPRESSION = _sub("#/$defs/suppression")
CATALOG = _sub("#/$defs/catalog")
RUNBOOK = _sub("#/$defs/runbook")
MAX_PACK_BYTES = 4 * 1024 * 1024
MAX_LIFETIME_DAYS = 366


@dataclass
class PackResult:
    errors: list[tuple[str, str]] = field(default_factory=list)
    skipped_rules: list[str] = field(default_factory=list)
    rules: dict[str, dict] = field(default_factory=dict)
    manifest: dict | None = None

    @property
    def ok(self) -> bool:
        return not self.errors

    @property
    def codes(self) -> set[str]:
        return {c for c, _ in self.errors}


@dataclass(frozen=True)
class Lineage:
    """The last-known-good pack on this site (C4 S5)."""

    id: str
    channel: str
    version: str


def _semver(v: str) -> tuple[int, int, int]:
    a, b, c = (int(x) for x in v.split("."))
    return a, b, c


def _ts(s: str) -> datetime:
    return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)


def _no_dupes(pairs: list) -> dict:
    keys = [k for k, _ in pairs]
    if len(keys) != len(set(keys)):
        raise ValueError(
            f"duplicate JSON keys {sorted({k for k in keys if keys.count(k) > 1})}"
        )
    return dict(pairs)


def _reject_constant(name: str) -> None:
    raise ValueError(f"non-standard JSON constant {name}")


def _yaml(text: str):
    return yaml.load(text, Loader=_StrictLoader)


def check_pack(
    data: bytes,
    *,
    vigil_version: str,
    now: datetime,
    last_known_good: Lineage | None = None,
    override_lineage: bool = False,
) -> PackResult:
    res = PackResult()
    err = res.errors.append

    if len(data) > MAX_PACK_BYTES:
        err(("P-SIZE", f"{len(data)} bytes > {MAX_PACK_BYTES}"))
        return res
    try:
        doc = json.loads(
            data.decode("utf-8"),
            object_pairs_hook=_no_dupes,
            parse_constant=_reject_constant,
        )
    except (ValueError, RecursionError, UnicodeDecodeError) as exc:
        err(("P-JSON", str(exc)[:200]))
        return res
    schema_errors = list(PACK.iter_errors(doc))
    if schema_errors:
        for e in schema_errors[:10]:
            err(("P-SCHEMA", f"{'/'.join(map(str, e.path))}: {e.message[:150]}"))
        return res
    m = res.manifest = doc["manifest"]
    files: dict[str, str] = doc["files"]

    # Files and hashes: one-to-one with the manifest, every byte accounted for.
    listed = {c["path"]: c for c in m["contents"]}
    if len(listed) != len(m["contents"]):
        err(("P-FILES", "a path is listed twice"))
    for path in sorted(set(files) ^ set(listed)):
        err(
            (
                "P-FILES",
                f"{path}: {'not in manifest' if path in files else 'listed but missing'}",
            )
        )
    for path, entry in listed.items():
        if path not in files:
            continue
        raw = files[path].encode("utf-8")
        if (
            hashlib.sha256(raw).hexdigest() != entry["sha256"]
            or len(raw) != entry["bytes"]
        ):
            err(("P-HASH", f"{path}: content doesn't match its manifest hash"))
        expected_kind = {"rules": "rule", "runbooks": "runbook"}.get(
            path.split("/")[0], path.removesuffix(".yaml")
        )
        if entry["kind"] != expected_kind:
            err(("P-FILES", f"{path}: kind {entry['kind']} doesn't match its location"))
    if res.errors:
        return res

    # Identity: dates, Vigil range, lineage (K1 T-22).
    p = m["pack"]
    created, expires = _ts(p["created_at"]), _ts(p["expires_at"])
    if not created < expires or (expires - created).days > MAX_LIFETIME_DAYS:
        err(
            (
                "P-DATES",
                f"expires_at must be after created_at and within {MAX_LIFETIME_DAYS} days",
            )
        )
    if expires <= now:
        err(("P-EXPIRED", f"pack expired at {p['expires_at']}"))
    lo, hi = _semver(m["vigil"]["min"]), _semver(m["vigil"]["max_exclusive"])
    if not lo < hi:
        err(("P-VIGIL", "vigil.min must be below vigil.max_exclusive"))
    elif not lo <= _semver(vigil_version) < hi:
        err(
            (
                "P-VIGIL",
                f"pack is for Vigil [{m['vigil']['min']}, {m['vigil']['max_exclusive']}), this is {vigil_version}",
            )
        )
    if last_known_good and not override_lineage:
        if (p["id"], p["channel"]) != (last_known_good.id, last_known_good.channel):
            err(
                (
                    "P-LINEAGE",
                    f"pack {p['id']}/{p['channel']} replaces {last_known_good.id}/{last_known_good.channel}",
                )
            )
        elif _semver(p["version"]) <= _semver(last_known_good.version):
            err(
                (
                    "P-DOWNGRADE",
                    f"version {p['version']} is not newer than {last_known_good.version}",
                )
            )

    # Rules: every one passes the E2 loader; ids unique; file named after the id.
    needed = (1, 0)
    for path in sorted(x for x in files if x.startswith("rules/")):
        r = check_text(files[path])
        if not r.ok:
            err(("P-RULE", f"{path}: {sorted(r.codes)}"))
            continue
        rule = _yaml(files[path])
        if path != f"rules/{rule['id'].replace('.', '-')}.yaml":
            err(
                (
                    "P-PATH",
                    f"{path}: rule {rule['id']} must live in rules/{rule['id'].replace('.', '-')}.yaml",
                )
            )
        if rule["id"] in res.rules:
            err(("P-RULE-DUP", f"rule id {rule['id']} appears twice"))
        res.rules[rule["id"]] = rule
        rule_api = tuple(int(x) for x in rule.get("engine_api", "1.0").split("."))
        needed = max(needed, rule_api)
        if r.skipped:
            res.skipped_rules.append(rule["id"])
    if f"{needed[0]}.{needed[1]}" != m["engine_api"]:
        err(
            (
                "P-ENGINE",
                f"manifest engine_api {m['engine_api']} must equal the highest rule need {needed[0]}.{needed[1]}",
            )
        )
    if int(m["engine_api"].split(".")[0]) != ENGINE_API[0]:
        err(("P-ENGINE", "engine API major mismatch"))

    # Suppression (E3 §6): schema, rule selectors exist, no cycles.
    if "suppression.yaml" in files:
        _check_suppression(_yaml(files["suppression.yaml"]), res)

    # Catalog (D3 §2): schema; every trusted-only log signal can match some entry.
    catalog = (
        _yaml(files["catalog.yaml"]) if "catalog.yaml" in files else {"entries": []}
    )
    if "catalog.yaml" in files:
        for e in CATALOG.iter_errors(catalog):
            err(("P-CATALOG", e.message[:150]))
    _check_catalog_reach(catalog.get("entries", []), res)

    # Runbooks (G4 slot): schema; file named after id; applies only to lane-1 rules.
    for path in sorted(x for x in files if x.startswith("runbooks/")):
        rb = _yaml(files[path])
        errs = list(RUNBOOK.iter_errors(rb))
        if errs:
            err(("P-RUNBOOK", f"{path}: {errs[0].message[:120]}"))
            continue
        if path != f"runbooks/{rb['id']}.yaml":
            err(
                (
                    "P-PATH",
                    f"{path}: runbook {rb['id']} must live in runbooks/{rb['id']}.yaml",
                )
            )
        for rid in rb["applies_to"]:
            if rid not in res.rules:
                err(("P-RUNBOOK", f"{rb['id']}: applies to unknown rule {rid}"))
            elif res.rules[rid]["lane"] != 1:
                err(
                    (
                        "P-RUNBOOK",
                        f"{rb['id']}: rule {rid} is lane {res.rules[rid]['lane']}, runbooks only fix lane 1",
                    )
                )

    # Counts are a cross-check a human can read in the manifest.
    expected = {
        "rules": sum(1 for x in files if x.startswith("rules/")),
        "runbooks": sum(1 for x in files if x.startswith("runbooks/")),
        "suppressions": len(_yaml(files["suppression.yaml"]).get("entries", []))
        if "suppression.yaml" in files
        else 0,
        "catalog_entries": len(catalog.get("entries", [])),
    }
    if expected != m["counts"]:
        err(("P-COUNTS", f"manifest counts {m['counts']} != actual {expected}"))
    return res


def _selects(sel: dict, rid: str, rule: dict) -> bool:
    key, val = next(iter(sel.items()))
    f = rule["fault"]
    return {
        "rule": rid == val,
        "class": f["class"] == val,
        "mode": f["mode"] == val,
        "cause": val in f["causes"],
    }[key]


def _check_suppression(doc: dict, res: PackResult) -> None:
    errs = list(SUPPRESSION.iter_errors(doc))
    if errs:
        res.errors.append(("P-SUPPRESSION", errs[0].message[:150]))
        return
    edges: dict[str, set[str]] = {rid: set() for rid in res.rules}
    for entry in doc["entries"]:
        for side in ("parent", "children"):
            sel = entry[side]
            if "rule" in sel and sel["rule"] not in res.rules:
                res.errors.append(
                    ("P-SUPPRESSION", f"{side} names unknown rule {sel['rule']}")
                )
        parents = [
            r for r, rule in res.rules.items() if _selects(entry["parent"], r, rule)
        ]
        children = [
            r for r, rule in res.rules.items() if _selects(entry["children"], r, rule)
        ]
        for a in parents:
            edges[a].update(children)
    # A cycle (including a rule suppressing itself) means two faults could hide each other.
    state: dict[str, int] = {}

    def visit(n: str) -> bool:
        state[n] = 1
        for m in edges[n]:
            if state.get(m) == 1 or (state.get(m) is None and visit(m)):
                return True
        state[n] = 2
        return False

    if any(state.get(n) is None and visit(n) for n in sorted(edges)):
        res.errors.append(("P-SUPPRESSION", "suppression graph has a cycle"))


def _matches(match: dict | None, value: str) -> bool:
    if match is None:
        return True
    kind, pat = next(iter(match.items()))
    if kind == "eq":
        return value == pat
    if kind == "prefix":
        return value.startswith(pat)
    return re2.search(pat, value) is not None


def _check_catalog_reach(entries: list, res: PackResult) -> None:
    for rid, rule in res.rules.items():
        for name, sig in rule["signals"].items():
            log = sig.get("log")
            if (
                not log
                or log.get("trusted_only", True) is False
                or "template" not in log
            ):
                continue
            if not any(
                _matches(log.get("logger"), e["logger"])
                and _matches(log["template"], e["template"])
                for e in entries
            ):
                res.errors.append(
                    (
                        "P-CATALOG",
                        f"{rid}/{name}: trusted-only log signal matches no catalog entry, so it can never fire",
                    )
                )
