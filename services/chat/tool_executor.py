"""Tool execution for the agentic chat loop.

Handles three dispatch paths:
- ``process_backend_tool_use``: calls Vigil's own DB/service tools (async)
- ``process_mcp_tool_use``: calls external MCP server tools (async)
- ``process_mixed_tool_use``: routes each tool call to the correct path

``ToolExecutor`` is stateful only in ``skill_tool_index``, which ClaudeService
refreshes before each request via ``_refresh_skill_tools``.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional

from services.chat.context_manager import ContextManager

logger = logging.getLogger(__name__)


class ToolExecutor:
    def __init__(self) -> None:
        self.skill_tool_index: Dict[str, Any] = {}

    # ------------------------------------------------------------------
    # Backend tool dispatch (async, complete handler set)
    # ------------------------------------------------------------------

    async def process_backend_tool_use(
        self, content: List, backend_tools: Optional[List] = None
    ) -> List[Dict]:
        """Iterate Anthropic content blocks, dispatch each tool_use to the
        appropriate backend handler, and return tool_result blocks."""
        tool_results: List[Dict] = []
        security_tools = None

        for item in content:
            if isinstance(item, dict):
                item_type = item.get("type")
                tool_name = item.get("name")
                tool_id = item.get("id")
                arguments = item.get("input", {})
            else:
                item_type = getattr(item, "type", None)
                tool_name = getattr(item, "name", None)
                tool_id = getattr(item, "id", None)
                arguments = getattr(item, "input", {})

            if item_type != "tool_use" or not tool_name:
                continue

            try:
                result = None

                # DB-backed Skills (Issue #82)
                if tool_name.startswith("skill_"):
                    try:
                        from services.skill_tools_bridge import execute_skill_tool

                        result = execute_skill_tool(
                            tool_name,
                            arguments or {},
                            skills_by_tool_name=self.skill_tool_index or None,
                        )
                    except Exception as exc:
                        logger.warning("Skill tool dispatch failed for %s: %s", tool_name, exc)
                        result = {"error": f"Skill execution failed: {exc}"}

                # Security detection tools
                if result is None and tool_name in (
                    "analyze_coverage",
                    "search_detections",
                    "identify_gaps",
                    "get_coverage_stats",
                    "get_detection_count",
                ):
                    if security_tools is None:
                        from tools.security_detections import get_security_detection_tools

                        security_tools = get_security_detection_tools()
                    handler = getattr(security_tools, tool_name)
                    result = await handler(**arguments)

                # Findings / cases
                elif tool_name in (
                    "list_findings",
                    "get_finding",
                    "nearest_neighbors",
                    "search_findings",
                    "get_findings_stats",
                    "list_cases",
                    "get_case",
                    "create_case",
                    "add_finding_to_case",
                    "update_case",
                    "add_resolution_step",
                ):
                    from services.database_data_service import DatabaseDataService

                    data_service = DatabaseDataService()
                    result = await _dispatch_findings_tool(
                        tool_name, arguments, data_service
                    )

                # ATT&CK tools
                elif tool_name in ("get_attack_layer", "get_technique_rollup"):
                    from services.database_data_service import DatabaseDataService

                    data_service = DatabaseDataService()
                    result = _dispatch_attack_tool(tool_name, arguments, data_service)

                # Approval tools
                elif tool_name in (
                    "list_pending_approvals",
                    "get_approval_action",
                    "approve_action",
                    "reject_action",
                    "get_approval_stats",
                ):
                    from services.approval_service import ApprovalService

                    result = _dispatch_approval_tool(
                        tool_name, arguments, ApprovalService()
                    )

                elif result is None:
                    logger.warning("Unknown backend tool: %s", tool_name)
                    result = {"error": f"Unknown tool: {tool_name}"}

                # Serialize + truncate + wrap
                content_str = (
                    json.dumps(result)
                    if isinstance(result, (dict, list))
                    else str(result)
                )
                content_str = ContextManager.truncate_tool_response(
                    content_str, tool_name=tool_name
                )
                from services.prompt_security import wrap_tool_result

                content_str = wrap_tool_result(
                    content_str, source="backend", tool=tool_name
                )
                tool_results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": tool_id,
                        "content": [{"type": "text", "text": content_str}],
                    }
                )
                logger.info("Executed backend tool: %s", tool_name)

            except Exception as exc:
                logger.error("Error calling backend tool %s: %s", tool_name, exc, exc_info=True)
                from services.prompt_security import wrap_tool_result

                err_text = wrap_tool_result(
                    f"Error: {exc}", source="backend", tool=tool_name
                )
                tool_results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": tool_id,
                        "content": [{"type": "text", "text": err_text}],
                    }
                )

        return tool_results

    # ------------------------------------------------------------------
    # MCP tool dispatch
    # ------------------------------------------------------------------

    async def process_mcp_tool_use(self, content: List) -> List[Dict]:
        """Call external MCP server tools and return tool_result blocks."""
        tool_results: List[Dict] = []

        for item in content:
            if isinstance(item, dict):
                item_type = item.get("type")
                tool_name = item.get("name")
                tool_id = item.get("id")
                arguments = item.get("input", {})
            else:
                item_type = getattr(item, "type", None)
                tool_name = getattr(item, "name", None)
                tool_id = getattr(item, "id", None)
                arguments = getattr(item, "input", {})

            if item_type != "tool_use" or not tool_name:
                continue

            parts = tool_name.split("_", 1)
            if len(parts) == 2:
                server_name, actual_tool_name = parts
            else:
                server_name = None
                actual_tool_name = tool_name
                from services.mcp_client import get_mcp_client

                mcp_client = get_mcp_client()
                if mcp_client:
                    for srv_name, tools in mcp_client.tools_cache.items():
                        if any(t["name"] == tool_name for t in tools):
                            server_name = srv_name
                            break

            if not server_name:
                logger.warning("Could not determine server for tool %s", tool_name)
                continue

            try:
                from services.mcp_client import get_mcp_client

                mcp_client = get_mcp_client()
                if mcp_client:
                    raw = await mcp_client.call_tool(
                        server_name, actual_tool_name, arguments, timeout=30.0
                    )
                    if isinstance(raw, dict):
                        blocks = raw.get("content", [{"type": "text", "text": str(raw)}])
                    else:
                        blocks = [{"type": "text", "text": str(raw)}]

                    from services.prompt_security import wrap_tool_result

                    for block in blocks:
                        if isinstance(block, dict) and block.get("type") == "text":
                            block["text"] = ContextManager.truncate_tool_response(
                                block["text"], tool_name=tool_name
                            )
                            block["text"] = wrap_tool_result(
                                block["text"],
                                source=server_name,
                                tool=actual_tool_name,
                            )

                    tool_results.append(
                        {
                            "type": "tool_result",
                            "tool_use_id": tool_id,
                            "content": blocks,
                        }
                    )
            except Exception as exc:
                logger.error("Error calling MCP tool %s: %s", tool_name, exc)
                from services.prompt_security import wrap_tool_result

                err_text = wrap_tool_result(
                    f"Error: {exc}",
                    source=server_name or "mcp",
                    tool=actual_tool_name,
                )
                tool_results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": tool_id,
                        "content": [{"type": "text", "text": err_text}],
                    }
                )

        return tool_results

    # ------------------------------------------------------------------
    # Mixed routing
    # ------------------------------------------------------------------

    async def process_mixed_tool_use(
        self,
        content: List,
        backend_tool_names: Optional[set] = None,
    ) -> List[Dict]:
        """Route each tool call to the backend or MCP processor."""
        tool_results: List[Dict] = []
        backend_names = backend_tool_names or set()

        for item in content:
            tool_name = (
                item.get("name") if isinstance(item, dict) else getattr(item, "name", None)
            )
            if not tool_name:
                continue
            if tool_name in backend_names:
                result = await self.process_backend_tool_use([item])
            else:
                result = await self.process_mcp_tool_use([item])
            tool_results.extend(result or [])

        return tool_results


# ------------------------------------------------------------------
# Private dispatch helpers (keep ToolExecutor class lean)
# ------------------------------------------------------------------


async def _dispatch_findings_tool(
    tool_name: str, arguments: Dict, data_service
) -> Any:
    """Handle all findings/cases backend tool calls."""
    if tool_name == "list_findings":
        limit = arguments.get("limit", 20)
        offset = arguments.get("offset", 0)
        severity = arguments.get("severity")
        data_source = arguments.get("data_source")
        status = arguments.get("status")
        total = data_service.count_findings(
            severity=severity, data_source=data_source, status=status
        )
        findings = data_service.get_findings(
            limit=limit,
            offset=offset,
            severity=severity,
            data_source=data_source,
            status=status,
            sort_by=arguments.get("sort_by", "timestamp"),
            sort_order=arguments.get("sort_order", "desc"),
        )
        compact = [
            {
                "finding_id": f.get("finding_id"),
                "severity": f.get("severity"),
                "anomaly_score": float(f.get("anomaly_score") or 0),
                "data_source": f.get("data_source"),
                "cluster_id": f.get("cluster_id"),
                "timestamp": f.get("timestamp"),
                "status": f.get("status"),
                "summary": (f.get("description") or "")[:200],
            }
            for f in findings
        ]
        return {
            "total": total,
            "offset": offset,
            "limit": limit,
            "has_more": (offset + limit) < total,
            "findings": compact,
        }

    if tool_name == "search_findings":
        query = arguments.get("query", "")
        limit = arguments.get("limit", 20)
        offset = arguments.get("offset", 0)
        severity = arguments.get("severity")
        data_source = arguments.get("data_source")
        status = arguments.get("status")
        total = data_service.count_findings(
            severity=severity,
            data_source=data_source,
            status=status,
            search_query=query,
        )
        findings = data_service.get_findings(
            limit=limit,
            offset=offset,
            severity=severity,
            data_source=data_source,
            status=status,
            search_query=query,
            sort_by=arguments.get("sort_by", "anomaly_score"),
            sort_order=arguments.get("sort_order", "desc"),
        )
        compact = [
            {
                "finding_id": f.get("finding_id"),
                "severity": f.get("severity"),
                "anomaly_score": float(f.get("anomaly_score") or 0),
                "data_source": f.get("data_source"),
                "timestamp": f.get("timestamp"),
                "status": f.get("status"),
                "summary": (f.get("description") or "")[:200],
            }
            for f in findings
        ]
        return {
            "query": query,
            "total": total,
            "offset": offset,
            "limit": limit,
            "has_more": (offset + limit) < total,
            "findings": compact,
        }

    if tool_name == "get_findings_stats":
        findings = data_service.get_findings(limit=10000)
        severity_counts: Dict = {}
        data_source_counts: Dict = {}
        status_counts: Dict = {}
        for f in findings:
            sev = f.get("severity") or "unknown"
            severity_counts[sev] = severity_counts.get(sev, 0) + 1
            ds = f.get("data_source") or "unknown"
            data_source_counts[ds] = data_source_counts.get(ds, 0) + 1
            st = f.get("status") or "unknown"
            status_counts[st] = status_counts.get(st, 0) + 1
        return {
            "total_findings": len(findings),
            "by_severity": severity_counts,
            "by_data_source": data_source_counts,
            "by_status": status_counts,
        }

    if tool_name == "get_finding":
        return data_service.get_finding(**arguments)

    if tool_name == "nearest_neighbors":
        return data_service.get_nearest_neighbors(**arguments)

    if tool_name == "list_cases":
        limit = arguments.get("limit", 50)
        status = arguments.get("status")
        severity = arguments.get("severity")
        cases = data_service.get_cases(limit=limit * 2)
        if status:
            cases = [c for c in cases if c.get("status") == status]
        if severity:
            cases = [c for c in cases if c.get("severity") == severity]
        return cases[:limit]

    if tool_name == "get_case":
        return data_service.get_case(**arguments)

    if tool_name == "create_case":
        return data_service.create_case(
            title=arguments["title"],
            finding_ids=arguments.get("finding_ids", []),
            priority=arguments.get("severity", "medium"),
            description=arguments.get("description", ""),
        )

    if tool_name == "add_finding_to_case":
        return data_service.add_finding_to_case(
            case_id=arguments["case_id"],
            finding_id=arguments["finding_id"],
        )

    if tool_name == "update_case":
        uc_args = dict(arguments)
        case_id = uc_args.pop("case_id")
        success = data_service.update_case(case_id, **uc_args)
        return {"success": success, "case_id": case_id}

    if tool_name == "add_resolution_step":
        from datetime import datetime as _dt

        case = data_service.get_case(arguments["case_id"])
        if not case:
            return {"error": f"Case {arguments['case_id']} not found"}
        res_steps = case.get("resolution_steps", [])
        res_steps.append(
            {
                "timestamp": _dt.utcnow().isoformat() + "Z",
                "description": arguments["description"],
                "action_taken": arguments["action_taken"],
                "result": arguments.get("result"),
            }
        )
        data_service.update_case(arguments["case_id"], resolution_steps=res_steps)
        return {
            "success": True,
            "case_id": arguments["case_id"],
            "total_steps": len(res_steps),
        }

    return {"error": f"Unhandled findings tool: {tool_name}"}


def _dispatch_attack_tool(tool_name: str, arguments: Dict, data_service) -> Any:
    if tool_name == "get_attack_layer":
        return {
            "success": True,
            "layer": {
                "name": "DeepTempo Findings",
                "version": "4.5",
                "domain": "enterprise-attack",
                "description": "ATT&CK techniques from findings",
                "techniques": [],
            },
        }
    if tool_name == "get_technique_rollup":
        min_conf = arguments.get("min_confidence", 0.0) if arguments else 0.0
        findings = data_service.get_findings(limit=1000)
        counts: Dict = {}
        severities: Dict = {}
        for f in findings:
            for tech in f.get("predicted_techniques", []) or []:
                tid = tech.get("technique_id")
                conf = tech.get("confidence", 0)
                if not tid or conf < min_conf:
                    continue
                counts[tid] = counts.get(tid, 0) + 1
                if tid not in severities:
                    severities[tid] = {"critical": 0, "high": 0, "medium": 0, "low": 0}
                sev = f.get("severity") or "medium"
                severities[tid][sev] = severities[tid].get(sev, 0) + 1
        techniques = [
            {"technique_id": t, "count": c, "severities": severities[t]}
            for t, c in counts.items()
        ]
        techniques.sort(key=lambda x: x["count"], reverse=True)
        return {
            "success": True,
            "total_techniques": len(techniques),
            "techniques": techniques,
        }
    return {"error": f"Unknown ATT&CK tool: {tool_name}"}


def _dispatch_approval_tool(
    tool_name: str, arguments: Dict, approval_service
) -> Any:
    from dataclasses import asdict

    if tool_name == "list_pending_approvals":
        actions = approval_service.list_pending_approvals()
        return [asdict(a) for a in actions[: arguments.get("limit", 50)]]

    if tool_name == "get_approval_action":
        action = approval_service.get_action(arguments["action_id"])
        return asdict(action) if action else {"error": "Action not found"}

    if tool_name == "approve_action":
        action = approval_service.approve_action(**arguments)
        return asdict(action) if action else {"error": "Action not found or cannot be approved"}

    if tool_name == "reject_action":
        action = approval_service.reject_action(**arguments)
        return asdict(action) if action else {"error": "Action not found or cannot be rejected"}

    if tool_name == "get_approval_stats":
        return approval_service.get_stats()

    return {"error": f"Unknown approval tool: {tool_name}"}
