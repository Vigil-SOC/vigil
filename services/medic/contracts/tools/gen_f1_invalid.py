"""Regenerates contracts/fixtures/packs/invalid/ from the built sample pack.
Run from contracts/: uv run python tools/gen_f1_invalid.py
Each case is one mutation; expected.json names the exact codes pack_check must report."""

import copy
import hashlib
import json
from pathlib import Path

import yaml

SAMPLE = json.loads(Path("fixtures/packs/sample-0.1.0.medicpack.json").read_text())
OUT = Path("fixtures/packs/invalid")
OUT.mkdir(parents=True, exist_ok=True)
CASES: dict[str, dict] = {}


def rehash(doc: dict) -> dict:
    """Make the manifest truthful again, so only the intended defect remains."""
    contents = []
    for path, text in sorted(doc["files"].items()):
        kind = {"rules": "rule", "runbooks": "runbook"}.get(
            path.split("/")[0], path.removesuffix(".yaml")
        )
        data = text.encode()
        contents.append(
            {
                "path": path,
                "kind": kind,
                "sha256": hashlib.sha256(data).hexdigest(),
                "bytes": len(data),
            }
        )
    doc["manifest"]["contents"] = contents
    f = doc["files"]
    entries = lambda name: (
        len((yaml.safe_load(f[name]) or {}).get("entries", [])) if name in f else 0
    )
    doc["manifest"]["counts"] = {
        "rules": sum(p.startswith("rules/") for p in f),
        "runbooks": sum(p.startswith("runbooks/") for p in f),
        "suppressions": entries("suppression.yaml"),
        "catalog_entries": entries("catalog.yaml"),
    }
    return doc


def emit(
    name: str,
    codes: list[str],
    why: str,
    mutate=None,
    raw: str | None = None,
    rehash_after: bool = True,
) -> None:
    if raw is None:
        doc = copy.deepcopy(SAMPLE)
        mutate(doc)
        if rehash_after:
            rehash(doc)
        raw = json.dumps(doc, sort_keys=True, separators=(",", ":")) + "\n"
    (OUT / f"{name}.medicpack.json").write_text(raw)
    CASES[name] = {"codes": codes, "why": why}


def edit(path: str, old: str, new: str):
    def f(doc):
        assert old in doc["files"][path], (path, old)
        doc["files"][path] = doc["files"][path].replace(old, new)

    return f


def setm(keys: list, value):
    def f(doc):
        node = doc["manifest"]
        for k in keys[:-1]:
            node = node[k]
        node[keys[-1]] = value

    return f


RULE = "rules/ingest-kafka-decode-errors.yaml"
emit(
    "tampered-byte",
    ["P-HASH"],
    "A byte changed after the manifest was written (lane 2 -> 1)",
    edit(RULE, "lane: 2", "lane: 1"),
    rehash_after=False,
)
emit(
    "unlisted-file",
    ["P-FILES"],
    "A file the manifest doesn't list",
    lambda d: d["files"].__setitem__("rules/extra.yaml", d["files"][RULE]),
    rehash_after=False,
)
emit(
    "missing-file",
    ["P-FILES"],
    "A listed file is missing",
    lambda d: d["files"].pop(RULE),
    rehash_after=False,
)
emit(
    "kind-mismatch",
    ["P-FILES"],
    "A rule file declared as a catalog",
    lambda d: next(
        c for c in d["manifest"]["contents"] if c["path"] == RULE
    ).__setitem__("kind", "catalog"),
    rehash_after=False,
)
emit(
    "path-traversal",
    ["P-SCHEMA"],
    "zip-slip style name (K1 T-25)",
    lambda d: d["files"].__setitem__("../rules/x.yaml", "x"),
)
emit(
    "absolute-path",
    ["P-SCHEMA"],
    "Absolute path",
    lambda d: d["files"].__setitem__("/etc/passwd", "x"),
)
emit(
    "unknown-manifest-field",
    ["P-SCHEMA"],
    "Strict schema: unknown fields refused",
    setm(["install_hook"], "curl x | sh"),
)
emit(
    "engine-api-overstated",
    ["P-ENGINE"],
    "Manifest must state exactly what the rules need",
    setm(["engine_api"], "1.2"),
)
emit(
    "expired",
    ["P-EXPIRED"],
    "Expired before import (K1 T-22)",
    lambda d: d["manifest"]["pack"].update(
        created_at="2026-01-01T00:00:00Z", expires_at="2026-10-01T00:00:00Z"
    ),
)
emit(
    "lifetime-too-long",
    ["P-DATES"],
    "Expiry more than 366 days after creation",
    lambda d: d["manifest"]["pack"].update(
        created_at="2026-10-01T00:00:00Z", expires_at="2028-01-01T00:00:00Z"
    ),
)
emit(
    "vigil-out-of-range",
    ["P-VIGIL"],
    "Written for a different Vigil",
    setm(["vigil"], {"min": "0.7.0", "max_exclusive": "0.8.0"}),
)
emit(
    "rule-with-code",
    ["P-RULE"],
    "One refused rule refuses the whole pack (E2 §6)",
    edit(RULE, "lane: 2\n", "lane: 2\nexpr: __import__('os').system('id')\n"),
)
emit(
    "rule-misfiled",
    ["P-PATH", "P-RULE-DUP"],
    "Same rule id twice, one in a file not named after it",
    lambda d: d["files"].__setitem__("rules/kafka-copy.yaml", d["files"][RULE]),
)
emit(
    "suppression-cycle",
    ["P-SUPPRESSION"],
    "llm suppresses P-4 and P-4 suppresses llm: two faults hide each other",
    edit(
        "suppression.yaml",
        "entries:\n",
        "entries:\n  - parent: {mode: P-4}\n    children: {class: llm}\n    why: Wrong way round.\n",
    ),
)
emit(
    "suppression-unknown-rule",
    ["P-SUPPRESSION"],
    "A rule selector must name a rule in the pack",
    edit(
        "suppression.yaml",
        "entries:\n",
        "entries:\n  - parent: {rule: pipeline.does-not-exist}\n    children: {mode: P-4}\n    why: Typo.\n",
    ),
)
emit(
    "catalog-frontend-logger",
    ["P-CATALOG"],
    "The browser-writable frontend logger is never trusted (B3-X1)",
    edit(
        "catalog.yaml",
        "entries:\n",
        "entries:\n  - {logger: frontend.app, template: 'Unhandled error: %s'}\n",
    ),
)
emit(
    "catalog-unreachable-rule",
    ["P-CATALOG"],
    "A trusted-only rule whose templates aren't in the catalog can never fire",
    edit(
        "catalog.yaml",
        "  - {logger: services.daemon.processor, template: 'Failed to connect LLM gateway, AI triage is skipped until it connects: %s'}\n",
        "",
    ),
)
emit(
    "runbook-on-lane3-rule",
    ["P-RUNBOOK"],
    "Runbooks only fix lane-1 rules",
    edit(
        "runbooks/restart-soc-daemon.yaml",
        "applies_to: [pipeline.daemon-processor-restart]",
        "applies_to: [pipeline.daemon-component-hung]",
    ),
)
emit(
    "counts-wrong",
    ["P-COUNTS"],
    "Manifest counts must match the contents",
    lambda d: d["manifest"]["counts"].update(catalog_entries=3),
    rehash_after=False,
)
emit(
    "duplicate-json-key",
    ["P-JSON"],
    "Duplicate keys: a reviewer and the loader could read different values",
    None,
    raw=json.dumps(SAMPLE, sort_keys=True, separators=(",", ":")).replace(
        '{"files":', '{"format":"medic.pack/v1","files":', 1
    )
    + "\n",
)
emit(
    "nan-constant",
    ["P-JSON"],
    "Non-standard JSON",
    None,
    raw=json.dumps(SAMPLE, sort_keys=True, separators=(",", ":")).replace(
        '"bytes":', '"bytes":NaN,"x":', 1
    )
    + "\n",
)

(OUT / "expected.json").write_text(json.dumps(CASES, indent=2, sort_keys=True) + "\n")
print(len(CASES), "invalid packs")
