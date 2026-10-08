"""Build a Medic content pack (medic.pack/v1) from a source directory.

Source layout (F5 content repo):
    pack.yaml                 identity, engine_api, fingerprint, Vigil range
    rules/<rule-id>.yaml      rule schema v1 (E2), dots in the id become dashes
    runbooks/<id>.yaml        descriptors (G4), optional
    suppression.yaml          dependency suppression (E3 §6), optional
    catalog.yaml              trusted template catalog (D3 §2), optional

Output: one JSON file, byte-for-byte reproducible from the same source, so a
reviewer can rebuild and compare before F5 signs it (F2 signs the bytes).
Usage: uv run python pack_build.py <src_dir> <out_file>
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import yaml
from jsonschema import Draft202012Validator

if __package__:  # inside the services.medic package (A3 F-16)
    from .rule_check import _StrictLoader
else:  # run from the contracts folder (tests, CLI)
    from rule_check import _StrictLoader

HERE = Path(__file__).parent
_SCHEMA = json.loads((HERE / "pack-manifest.schema.json").read_text())


def _sub(ref: str) -> Draft202012Validator:
    return Draft202012Validator(
        {"$schema": _SCHEMA["$schema"], "$defs": _SCHEMA["$defs"], "$ref": ref}
    )


_SOURCE = _sub("#/$defs/source_manifest")
KINDS = {
    "rules": "rule",
    "runbooks": "runbook",
    "suppression.yaml": "suppression",
    "catalog.yaml": "catalog",
}


class BuildError(Exception):
    pass


def _kind(path: str) -> str:
    return KINDS[path.split("/")[0]]


def _entries(kind: str, text: str) -> int:
    doc = yaml.load(text, Loader=_StrictLoader)
    return len(doc.get("entries", [])) if kind in ("suppression", "catalog") else 1


def build(src: Path) -> bytes:
    source = yaml.load((src / "pack.yaml").read_text(), Loader=_StrictLoader)
    errors = [e.message for e in _SOURCE.iter_errors(source)]
    if errors:
        raise BuildError(f"pack.yaml: {errors}")
    files: dict[str, str] = {}
    for p in sorted(src.rglob("*")):
        if p.is_dir() or p.name == "pack.yaml":
            continue
        rel = p.relative_to(src).as_posix()
        if rel.split("/")[0] not in KINDS:
            raise BuildError(f"stray file in pack source: {rel}")
        files[rel] = p.read_text(encoding="utf-8")
    contents, counts = (
        [],
        {"rules": 0, "runbooks": 0, "suppressions": 0, "catalog_entries": 0},
    )
    for rel, text in files.items():
        data = text.encode("utf-8")
        kind = _kind(rel)
        contents.append(
            {
                "path": rel,
                "kind": kind,
                "sha256": hashlib.sha256(data).hexdigest(),
                "bytes": len(data),
            }
        )
        key = {
            "rule": "rules",
            "runbook": "runbooks",
            "suppression": "suppressions",
            "catalog": "catalog_entries",
        }[kind]
        counts[key] += _entries(kind, text)
    manifest = {
        "pack": source["pack"],
        "engine_api": source["engine_api"],
        "fingerprint": source["fingerprint"],
        "vigil": source["vigil"],
        "contents": contents,
        "counts": counts,
    }
    doc = {"format": source["format"], "manifest": manifest, "files": files}
    return (
        json.dumps(doc, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
        + "\n"
    ).encode("utf-8")


if __name__ == "__main__":
    out = build(Path(sys.argv[1]))
    Path(sys.argv[2]).write_bytes(out)
    print(f"{sys.argv[2]}: {len(out)} bytes, sha256 {hashlib.sha256(out).hexdigest()}")
