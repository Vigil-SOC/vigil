#!/usr/bin/env python3
"""Write core/threat_intel/attack_techniques.json from MITRE's Enterprise ATT&CK bundle.

One pinned release is downloaded from mitre-attack/attack-stix-data and cut down to
what Vigil reads offline: each technique and sub-technique id with its name, tactics,
status and, for a revoked one, the technique that replaced it. To move to a newer
release, change ATTACK_VERSION, run this script and commit the result.

    python scripts/generate_attack_techniques.py [path/to/enterprise-attack-X.json]
"""

import json
import sys
import urllib.request
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
    ids = {}
    techniques = {}
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
            "tactics": [p["phase_name"] for p in obj.get("kill_chain_phases", [])],
            "status": status,
        }
    for obj in objects:
        if obj.get("type") != "relationship" or obj.get("relationship_type") != (
            "revoked-by"
        ):
            continue
        old, new = ids.get(obj.get("source_ref")), ids.get(obj.get("target_ref"))
        if old and new and techniques[old]["status"] == "revoked":
            techniques[old]["replaced_by"] = new
    return {"version": ATTACK_VERSION, "techniques": techniques}


def main() -> None:
    if len(sys.argv) > 1:
        bundle = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    else:
        with urllib.request.urlopen(SOURCE_URL, timeout=300) as response:
            bundle = json.load(response)
    data = build(bundle)
    OUTPUT.write_text(
        json.dumps(data, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    print(f"{len(data['techniques'])} techniques -> {OUTPUT}")


if __name__ == "__main__":
    main()
