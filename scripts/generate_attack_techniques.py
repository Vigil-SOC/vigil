#!/usr/bin/env python3
"""Write core/threat_intel/attack_techniques.json from MITRE's ATT&CK bundle and ATLAS release.

One pinned release of MITRE's bundle (mitre-attack/attack-stix-data) is cut down to
what Vigil reads offline: each technique and sub-technique id with its name, tactics,
status and, for a revoked one, the active technique that finally replaced it.

One pinned MITRE ATLAS release (mitre-atlas/atlas-data, dist/v6) goes under its own
"atlas" key with its version: each technique and sub-technique id with its name. ATLAS v6
carries no revoked or deprecated flag, so every entry is written active.

The script fetches nothing. Download enterprise-attack-<ATTACK_VERSION>.json from
SOURCE_URL and ATLAS-<ATLAS_VERSION>.yaml from ATLAS_SOURCE_URL first, then:

    python scripts/generate_attack_techniques.py \
        --bundle path/to/enterprise-attack-X.json --atlas path/to/ATLAS-Y.yaml

To move to a newer release, change ATTACK_VERSION or ATLAS_VERSION, download that file,
run this and commit the result (and update ATTACK_NOTICE.md / ATLAS_NOTICE.md).
"""

import argparse
import json
from pathlib import Path

import yaml

ATTACK_VERSION = "20.1"
SOURCE_URL = (
    "https://raw.githubusercontent.com/mitre-attack/attack-stix-data/master/"
    f"enterprise-attack/enterprise-attack-{ATTACK_VERSION}.json"
)
ATLAS_VERSION = "2026.10"
ATLAS_SOURCE_URL = (
    "https://raw.githubusercontent.com/mitre-atlas/atlas-data/main/dist/v6/"
    f"ATLAS-{ATLAS_VERSION}.yaml"
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
    return {
        "version": ATTACK_VERSION,
        # The matrix's tactics in kill-chain order, as the heat map's columns (#4103).
        "tactics": sorted(order, key=order.__getitem__),
        "techniques": techniques,
    }


def build_atlas(release: dict) -> dict:
    version = str(release["collection"]["version"])
    if version != ATLAS_VERSION:
        raise SystemExit(f"ATLAS file is {version}, expected {ATLAS_VERSION}")
    # Only the top-level map: the case studies list techniques again.
    return {
        "version": version,
        "techniques": {
            technique_id: {"name": entry["name"], "status": "active"}
            for technique_id, entry in release["techniques"].items()
            if technique_id.startswith("AML.T")
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument(
        "--bundle",
        required=True,
        type=Path,
        help=f"local enterprise-attack-{ATTACK_VERSION}.json, downloaded from {SOURCE_URL}",
    )
    parser.add_argument(
        "--atlas",
        required=True,
        type=Path,
        help=f"local ATLAS-{ATLAS_VERSION}.yaml, downloaded from {ATLAS_SOURCE_URL}",
    )
    args = parser.parse_args()
    data = build(json.loads(args.bundle.read_text(encoding="utf-8")))
    data["atlas"] = build_atlas(yaml.safe_load(args.atlas.read_text(encoding="utf-8")))
    OUTPUT.write_text(
        json.dumps(data, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    print(
        f"{len(data['techniques'])} ATT&CK and {len(data['atlas']['techniques'])} "
        f"ATLAS techniques -> {OUTPUT}"
    )


if __name__ == "__main__":
    main()
