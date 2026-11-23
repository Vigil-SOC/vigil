"""Seed a fresh OpenCTI with the intel the opencti live check reads.

``python3 opencti_seed.py URL TOKEN`` creates one small, connected set of objects
and prints ``OPENCTI_LIVE_<NAME>=<id>`` lines (opencti.sh appends them to the
live env file):

- intrusion set "Vigil Live Lynx" ``uses`` malware "VigilLiveLoader" and attack
  pattern T1071.001;
- the malware ``uses`` T1059.001;
- indicator ``[ipv4-addr:value = '203.0.113.88']`` is ``based-on`` that IPv4
  observable and ``indicates`` the malware;
- one report holds all six objects.

Standard library only, so it runs before any dependency is installed.
"""

import json
import sys
import time
import urllib.request

IP = "203.0.113.88"


def main(url: str, token: str) -> None:
    def query(q: str) -> dict:
        req = urllib.request.Request(
            f"{url}/graphql",
            data=json.dumps({"query": q}).encode(),
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
        )
        with urllib.request.urlopen(req, timeout=60) as resp:
            body = json.load(resp)
        if body.get("errors"):
            raise SystemExit(f"seed failed: {body['errors'][0].get('message')}")
        return body["data"]

    def add(mutation: str, field: str) -> str:
        return query(f"mutation {{ {field}({mutation}) {{ id }} }}")[field]["id"]

    ids = {
        "INTRUSION_SET": add(
            'input: {name: "Vigil Live Lynx", description: "Live-check intrusion set"}',
            "intrusionSetAdd",
        ),
        "MALWARE": add(
            'input: {name: "VigilLiveLoader", is_family: true}', "malwareAdd"
        ),
        "T1071": add(
            'input: {name: "Web Protocols", x_mitre_id: "T1071.001"}',
            "attackPatternAdd",
        ),
        "T1059": add(
            'input: {name: "PowerShell", x_mitre_id: "T1059.001"}', "attackPatternAdd"
        ),
        "OBSERVABLE": add(
            f'type: "IPv4-Addr", IPv4Addr: {{value: "{IP}"}}', "stixCyberObservableAdd"
        ),
        "INDICATOR": add(
            f'input: {{name: "{IP}", pattern: "[ipv4-addr:value = \'{IP}\']", pattern_type: "stix",'
            ' x_opencti_main_observable_type: "IPv4-Addr", x_opencti_score: 80}',
            "indicatorAdd",
        ),
    }
    for source, target, kind in (
        ("INTRUSION_SET", "MALWARE", "uses"),
        ("INTRUSION_SET", "T1071", "uses"),
        ("MALWARE", "T1059", "uses"),
        ("INDICATOR", "OBSERVABLE", "based-on"),
        ("INDICATOR", "MALWARE", "indicates"),
    ):
        add(
            f'input: {{fromId: "{ids[source]}", toId: "{ids[target]}", relationship_type: "{kind}"}}',
            "stixCoreRelationshipAdd",
        )
    objects = ", ".join(f'"{i}"' for i in ids.values())
    ids["REPORT"] = add(
        'input: {name: "Vigil Live Lynx: VigilLiveLoader beacons over HTTP",'
        ' published: "2026-01-01T00:00:00Z", report_types: ["threat-report"],'
        f" objects: [{objects}]}}",
        "reportAdd",
    )
    # The probes search by text; wait until the search index serves what was written.
    for _ in range(30):
        found = query(
            f'{{ stixCyberObservables(search: "{IP}") {{ edges {{ node {{ id }} }} }}'
            ' attackPatterns(search: "Web Protocols") { edges { node { id } } } }'
        )
        if all(found[k]["edges"] for k in ("stixCyberObservables", "attackPatterns")):
            break
        time.sleep(2)
    else:
        raise SystemExit("seeded intel never became searchable")
    for name, value in ids.items():
        print(f"OPENCTI_LIVE_{name}={value}")


if __name__ == "__main__":
    main(*sys.argv[1:3])
