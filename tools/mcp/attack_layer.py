import asyncio
import json
import logging

import mcp.server.stdio
import mcp.types as types
from mcp.server import NotificationOptions, Server
from mcp.server.models import InitializationOptions

logger = logging.getLogger(__name__)


def result(data):
    return [types.TextContent(type="text", text=json.dumps(data, indent=2))]


def get_db():
    from core.storage.service import DatabaseService

    return DatabaseService()


async def handle_list_tools():
    return [
        types.Tool(
            name="get_technique_rollup",
            description="Get technique stats across findings",
            inputSchema={
                "type": "object",
                "properties": {"min_confidence": {"type": "number", "default": 0.0}},
                "required": [],
            },
        ),
        types.Tool(
            name="get_findings_by_technique",
            description="Get findings for a MITRE technique",
            inputSchema={
                "type": "object",
                "properties": {"technique_id": {"type": "string"}},
                "required": ["technique_id"],
            },
        ),
        types.Tool(
            name="create_attack_layer",
            description="Create ATT&CK layer from findings/case",
            inputSchema={
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "description": {"type": "string", "default": ""},
                    "case_id": {"type": "string"},
                    "finding_ids": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["name"],
            },
        ),
    ]


async def handle_call_tool(name: str, arguments: dict | None):
    try:
        db = get_db()
    except Exception as e:
        return result({"error": f"Database error: {e}"})

    try:
        if name == "get_technique_rollup":
            min_conf = arguments.get("min_confidence", 0.0) if arguments else 0.0
            findings = db.get_findings(limit=1000)

            counts = {}
            severities = {}
            for f in findings:
                for tech in f.predicted_techniques or []:
                    tid = tech.get("technique_id")
                    conf = tech.get("confidence", 0)
                    if conf < min_conf or not tid:
                        continue
                    counts[tid] = counts.get(tid, 0) + 1
                    if tid not in severities:
                        severities[tid] = {
                            "critical": 0,
                            "high": 0,
                            "medium": 0,
                            "low": 0,
                        }
                    sev = f.severity or "medium"
                    severities[tid][sev] = severities[tid].get(sev, 0) + 1

            techniques = [
                {"technique_id": t, "count": c, "severities": severities[t]}
                for t, c in counts.items()
            ]
            techniques.sort(key=lambda x: x["count"], reverse=True)
            return result(
                {
                    "success": True,
                    "total_techniques": len(techniques),
                    "techniques": techniques,
                }
            )

        elif name == "get_findings_by_technique":
            tid = arguments.get("technique_id") if arguments else None
            if not tid:
                return result({"error": "technique_id required"})

            findings = db.get_findings(limit=1000)
            matches = []
            for f in findings:
                for tech in f.predicted_techniques or []:
                    if tech.get("technique_id") == tid:
                        matches.append(
                            {
                                "finding_id": f.finding_id,
                                "severity": f.severity,
                                "data_source": f.data_source,
                                "timestamp": f.timestamp,
                            }
                        )
                        break
            return result(
                {
                    "success": True,
                    "technique_id": tid,
                    "total": len(matches),
                    "findings": matches,
                }
            )

        elif name == "create_attack_layer":
            name_arg = arguments.get("name") if arguments else None
            desc = arguments.get("description", "") if arguments else ""
            case_id = arguments.get("case_id") if arguments else None
            finding_ids = arguments.get("finding_ids") if arguments else None

            if not name_arg:
                return result({"error": "name required"})

            findings = []
            if case_id:
                case = db.get_case(case_id)
                if case and hasattr(case, "findings"):
                    findings = list(case.findings)
            elif finding_ids:
                findings = [db.get_finding(fid) for fid in finding_ids]
                findings = [f for f in findings if f]
            else:
                findings = db.get_findings(limit=500)

            tech_data = {}
            for f in findings:
                for tech in f.predicted_techniques or []:
                    tid = tech.get("technique_id")
                    conf = tech.get("confidence", 0)
                    if not tid:
                        continue
                    if tid not in tech_data:
                        tech_data[tid] = {"techniqueID": tid, "score": 0, "count": 0}
                    tech_data[tid]["count"] += 1
                    tech_data[tid]["score"] = max(tech_data[tid]["score"], conf)

            max_count = (
                max([t["count"] for t in tech_data.values()]) if tech_data else 1
            )
            for t in tech_data.values():
                ratio = t["count"] / max_count
                t["color"] = (
                    "#ff0000"
                    if ratio >= 0.75
                    else "#ff9900" if ratio >= 0.5 else "#ffff00"
                )
                t["comment"] = f"Detected {t['count']}x, max conf {t['score']:.2f}"

            layer = {
                "name": name_arg,
                "version": "4.5",
                "domain": "enterprise-attack",
                "description": desc or f"Layer from {len(findings)} findings",
                "techniques": list(tech_data.values()),
            }
            return result(
                {
                    "success": True,
                    "layer": layer,
                    "technique_count": len(tech_data),
                    "finding_count": len(findings),
                }
            )

        else:
            return result({"error": f"Unknown tool: {name}"})

    except Exception as e:
        return result({"error": str(e)})


async def _on_list_tools(_ctx, _params):
    return types.ListToolsResult(tools=await handle_list_tools())


async def _on_call_tool(_ctx, params):
    try:
        content = await handle_call_tool(params.name, params.arguments)
    except Exception as exc:
        return types.CallToolResult(
            content=[types.TextContent(type="text", text=str(exc))],
            is_error=True,
        )
    return types.CallToolResult(content=content)


server = Server(
    "attack-layer",
    on_list_tools=_on_list_tools,
    on_call_tool=_on_call_tool,
)


async def main():
    async with mcp.server.stdio.stdio_server() as (read, write):
        await server.run(
            read,
            write,
            InitializationOptions(
                server_name="attack-layer",
                server_version="0.1.0",
                capabilities=server.get_capabilities(
                    notification_options=NotificationOptions(),
                    experimental_capabilities={},
                ),
            ),
        )


if __name__ == "__main__":
    asyncio.run(main())
