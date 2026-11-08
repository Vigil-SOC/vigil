#!/usr/bin/env python3
"""Write core/threat_intel/attack_techniques.json from MITRE's Enterprise ATT&CK bundle.

One pinned release of MITRE's bundle (mitre-attack/attack-stix-data) is cut down to
what Vigil reads offline: each technique and sub-technique id with its name, tactics,
status and, for a revoked one, the active technique that finally replaced it.

The script fetches nothing. Download enterprise-attack-<ATTACK_VERSION>.json from
SOURCE_URL first, then:

    python scripts/generate_attack_techniques.py --bundle path/to/enterprise-attack-X.json

To move to a newer release, change ATTACK_VERSION, download that file, run this and
commit the result.
"""

import argparse
import json
from pathlib import Path

ATTACK_VERSION = "20.1"
SOURCE_URL = (
    "https://raw.githubusercontent.com/mitre-attack/attack-stix-data/master/"
    f"enterprise-attack/enterprise-attack-{ATTACK_VERSION}.json"
)
OUTPUT = (
    Path(__file__).resolve().parent.parent
    / "core"
    / "threat_intel"
    / "attack_techniques.json"
)


def _external_id(obj: dict) -> str:
    for ref in obj.get("external_references", []):
        if ref.get("source_name") == "mitre-attack" and ref.get("external_id"):
            return ref["external_id"]
    return ""


def build(bundle: dict) -> dict:
    objects = bundle["objects"]
    tactic_names = {
        o["id"]: o["x_mitre_shortname"]
        for o in objects
        if o.get("type") == "x-mitre-tactic"
    }
    # The matrix lists its tactics in kill-chain order; each technique's follow it.
    matrix = next(o for o in objects if o.get("type") == "x-mitre-matrix")
    order = {
        tactic_names[ref]: i
        for i, ref in enumerate(matrix["tactic_refs"])
        if ref in tactic_names
    }
    ids = {}
    techniques = {}
    replaced_by = {}
    for obj in objects:
        if obj.get("type") != "attack-pattern":
            continue
        technique_id = _external_id(obj)
        if not technique_id.startswith("T"):
            continue
        ids[obj["id"]] = technique_id
        if obj.get("revoked"):
            status = "revoked"
        elif obj.get("x_mitre_deprecated"):
            status = "deprecated"
        else:
            status = "active"
        techniques[technique_id] = {
            "name": obj["name"],
            "tactics": sorted(
                {p["phase_name"] for p in obj.get("kill_chain_phases", [])},
                key=lambda name: (order.get(name, len(order)), name),
            ),
            "status": status,
        }
    for obj in objects:
        if obj.get("type") != "relationship" or obj.get("relationship_type") != (
            "revoked-by"
        ):
            continue
        old, new = ids.get(obj.get("source_ref")), ids.get(obj.get("target_ref"))
        if old and new and techniques[old]["status"] == "revoked":
            replaced_by[old] = new
    for old, new in replaced_by.items():
        # A replacement may itself be revoked: follow it to the active id.
        seen = {old}
        while techniques[new]["status"] == "revoked" and new in replaced_by:
            if new in seen:
                break
            seen.add(new)
            new = replaced_by[new]
        techniques[old]["replaced_by"] = new
    return {"version": ATTACK_VERSION, "techniques": techniques}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument(
        "--bundle",
        required=True,
        type=Path,
        help=f"local enterprise-attack-{ATTACK_VERSION}.json, downloaded from {SOURCE_URL}",
    )
    args = parser.parse_args()
    data = build(json.loads(args.bundle.read_text(encoding="utf-8")))
    OUTPUT.write_text(
        json.dumps(data, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    print(f"{len(data['techniques'])} techniques -> {OUTPUT}")


if __name__ == "__main__":
    main()
