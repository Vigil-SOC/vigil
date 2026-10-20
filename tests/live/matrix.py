"""Merge live-check reports into the connector matrix.

``python -m tests.live.matrix OUT.json REPORT.json...`` writes the merged matrix
to ``OUT.json`` and prints it as a Markdown table (the workflow sends that to the
job summary). Every connector in ``mcp-config.json`` gets a row: one with no
probe reads ``no live check`` instead of being left out, so the table never
implies coverage it does not have.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

# Worst first: a connector with one failing probe is failing.
_RANK = {"fail": 0, "pass": 1, "not-configured": 2}
_LABEL = {
    "fail": "❌ fail",
    "pass": "✅ pass",
    "not-configured": "⚪ not configured",
    "no live check": "— no live check",
}


def _servers() -> dict[str, dict]:
    return {
        name: entry
        for name, entry in json.loads((REPO_ROOT / "mcp-config.json").read_text())[
            "mcpServers"
        ].items()
        if isinstance(entry, dict)
    }


def connector_version(server: str) -> str:
    """What ``mcp-config.json`` pins for ``server``: an upstream version, ``in-repo``,
    ``hosted`` (a vendor's remote server, reached through mcp-remote), or ``unpinned``."""
    entry = _servers()[server]
    args = entry.get("args", [])
    if any(arg.startswith("mcp-remote@") for arg in args):  # its pin is the proxy's, not the vendor's
        return "hosted"
    for i, arg in enumerate(args):
        if arg.startswith("-") or (i and args[i - 1] == "--with"):  # --with: a helper, not the connector
            continue
        name = arg.rsplit("/", 1)[-1]
        if "@sha256:" in name:  # an image digest: enough of it to tell two apart
            return name[: name.index("@sha256:") + 20]
        if re.search(r"(?:@|==)v?\d[\w.\-]*$", name):
            return name
    if entry.get("command") == "python3":
        return "in-repo"
    return "unpinned"


def merge(reports: list[dict]) -> dict:
    servers = list(_servers())
    probes: dict[str, list[dict]] = {}
    for report in reports:
        for result in report["results"]:
            probes.setdefault(result["server"], []).append(result)

    rows = []
    for server in servers:
        results = probes.get(server, [])
        status = (
            min((r["status"] for r in results), key=_RANK.__getitem__)
            if results
            else "no live check"
        )
        rows.append(
            {
                "server": server,
                "status": status,
                "version": results[0]["version"] if results else connector_version(server),
                "probes": results,
            }
        )
    return {
        "commit": next((r["commit"] for r in reports), None),
        "ran_at": max((r["ran_at"] for r in reports), default=None),
        "connectors": rows,
    }


def markdown(matrix: dict) -> str:
    lines = [
        f"### Live connector checks ({matrix['ran_at']}, {matrix['commit']})",
        "",
        "| Connector | Version | Result | Transport | Evidence |",
        "|---|---|---|---|---|",
    ]
    for row in sorted(matrix["connectors"], key=lambda r: (r["status"] == "no live check", r["server"])):
        evidence = "<br>".join(
            f"`{p['tool']}`: {p['detail']}".replace("|", "\\|") for p in row["probes"]
        )
        # runner or stdio, from the session each probe connected through.
        transport = ", ".join(sorted({p["transport"] for p in row["probes"] if p.get("transport")}))
        lines.append(
            f"| {row['server']} | {row['version'] or ''} | {_LABEL[row['status']]} | {transport} | {evidence} |"
        )
    return "\n".join(lines) + "\n"


def main(argv: list[str]) -> int:
    out, *paths = argv
    reports = [json.loads(Path(p).read_text()) for p in paths if Path(p).is_file()]
    matrix = merge(reports)
    Path(out).write_text(json.dumps(matrix, indent=2) + "\n")
    print(markdown(matrix))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
