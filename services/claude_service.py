"""Claude API service for Anthropic integration with Agent SDK support."""

import asyncio
import base64
import json
import logging
import platform
import sys
import threading
import uuid
from pathlib import Path
from typing import Any, AsyncIterator, Dict, List, Optional, Union

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))
from secrets_manager import get_secret, set_secret

# Import backend tool support
try:
    sys.path.insert(0, str(Path(__file__).parent.parent))
    from backend.schemas.tool_schemas import ALL_TOOLS as BACKEND_TOOLS
    from tools.security_detections import get_security_detection_tools

    BACKEND_TOOLS_AVAILABLE = True
except ImportError as e:
    logging.warning(f"Backend tools not available: {e}")
    BACKEND_TOOLS = []
    BACKEND_TOOLS_AVAILABLE = False

try:
    from anthropic import Anthropic, AsyncAnthropic

    ANTHROPIC_AVAILABLE = True
except ImportError:
    ANTHROPIC_AVAILABLE = False

# OTEL instrumentation (lazy to avoid hard dependency)
try:
    from core.telemetry import get_tracer, get_meter, create_genai_metrics
    from opentelemetry.trace import SpanKind, StatusCode as _SpanStatusCode

    _cs_tracer = get_tracer("vigil.services.claude")
    _cs_meter = get_meter("vigil.services.claude")
    _cs_genai_metrics = create_genai_metrics(_cs_meter)
    _OTEL_CS_AVAILABLE = True
except Exception:
    _OTEL_CS_AVAILABLE = False

try:
    from claude_agent_sdk import ClaudeAgentOptions
    from claude_agent_sdk import query as agent_query

    AGENT_SDK_AVAILABLE = True
except ImportError:
    AGENT_SDK_AVAILABLE = False

logger = logging.getLogger(__name__)


class ClaudeService:
    """Service for interacting with Claude API with Agent SDK support."""

    SERVICE_NAME = "deeptempo-ai-soc"
    API_KEY_NAME = "claude_api_key"

    def __init__(
        self,
        use_mcp_tools: bool = True,
        enable_thinking: bool = False,
        thinking_budget: int = 10000,
        use_agent_sdk: bool = True,
        use_backend_tools: bool = False,
        provider_api_key_ref: Optional[str] = None,
    ):
        """
        Initialize Claude service.

        Args:
            use_mcp_tools: Whether to enable MCP tool integration
            enable_thinking: Whether to enable extended thinking (default: False)
            thinking_budget: Token budget for extended thinking (default: 10000)
            use_agent_sdk: Whether to use Claude Agent SDK for agentic workflows
            use_backend_tools: Whether to use backend function calling (bypasses MCP)
            provider_api_key_ref: Optional secret-manager key for a non-default
                Anthropic provider row (GH #88). When set, _load_api_key reads
                this secret first before the legacy CLAUDE_API_KEY fallback chain.
        """
        self.client: Optional[Anthropic] = None
        self.async_client: Optional[AsyncAnthropic] = None
        self.api_key: Optional[str] = None
        self.provider_api_key_ref = provider_api_key_ref
        self.use_mcp_tools = use_mcp_tools
        self.use_backend_tools = use_backend_tools
        self.mcp_tools: List[Dict] = []
        self.backend_tools: List[Dict] = []
        self.enable_thinking = enable_thinking
        self.thinking_budget = thinking_budget
        self.use_agent_sdk = use_agent_sdk and AGENT_SDK_AVAILABLE

        # Session management for multi-turn conversations
        # L1: in-memory (fast); L2: MemPalace (durable across restarts)
        self.sessions: Dict[str, List[Dict]] = {}
        self._mempalace = None  # lazy-initialized on first session access

        # Default system prompt with Claude 4.5 best practices
        self.default_system_prompt = self._get_default_system_prompt()

        # Try to load API key
        self._load_api_key()

        # Load backend tools if enabled
        if self.use_backend_tools and BACKEND_TOOLS_AVAILABLE:
            self._load_backend_tools()
            logger.info(
                f"✓ Loaded {len(self.backend_tools)} backend tools for direct function calling"
            )
        # Load MCP tools if enabled (independently of backend tools)
        if self.use_mcp_tools:
            self._load_mcp_tools()

        if self.use_agent_sdk:
            logger.info("Claude Agent SDK enabled for agentic workflows")

    def _get_default_system_prompt(self) -> str:
        """Get default system prompt with Claude 4.5 best practices."""
        return """You are Claude, an AI assistant for security operations and analysis in the Vigil SOC platform.

<default_to_action>
By default, implement changes rather than only suggesting them. If the user's intent is unclear, infer the most useful likely action and proceed, using tools to discover any missing details instead of guessing. Try to infer the user's intent about whether a tool call (e.g., file edit or read) is intended or not, and act accordingly.
</default_to_action>

<use_parallel_tool_calls>
If you intend to call multiple tools and there are no dependencies between the tool calls, make all of the independent tool calls in parallel. Prioritize calling tools simultaneously whenever the actions can be done in parallel rather than sequentially. For example, when reading 3 files, run 3 tool calls in parallel to read all 3 files into context at the same time. Maximize use of parallel tool calls where possible to increase speed and efficiency. However, if some tool calls depend on previous calls to inform dependent values like the parameters, do NOT call these tools in parallel and instead call them sequentially. Never use placeholders or guess missing parameters in tool calls.
</use_parallel_tool_calls>

<investigate_before_answering>
Never speculate about data you have not retrieved. If the user references a specific finding, case, or other security entity, you MUST use the appropriate MCP tool to fetch it before answering. Make sure to investigate and retrieve relevant data BEFORE answering questions. Never make any claims about security data before investigating - give grounded and hallucination-free answers.
</investigate_before_answering>

<available_mcp_tools>
You have access to MCP (Model Context Protocol) tools that connect to various security platforms and data sources. The tools are prefixed with the server name (e.g., "deeptempo-findings_get_finding"). Use these tools to:

1. **Findings & Cases**: Retrieve and analyze security findings and cases from DeepTempo
   - Finding IDs start with "f-" (e.g., "f-20260109-40d9379b")
   - Case IDs start with "case-" (e.g., "case-20260114-a1b2c3d4")
   - Use deeptempo-findings server tools: list_findings, get_finding, list_cases, get_case, create_case, update_case

2. **Security Integrations**: Query data from various security platforms
   - The available integrations are dynamically loaded based on what's configured
   - Tools are named with the pattern: {integration-name}_{tool-name}
   - Check your available tools to see which integrations are active

3. **Threat Intelligence**: Analyze indicators, URLs, files, etc.
   - Use tools for VirusTotal, Shodan, AnyRun, Hybrid Analysis, etc. (if available)
   - These help enrich findings with external context

4. **Investigation Workflows**: Execute predefined investigation workflows
   - Automate common SOC investigation patterns
   - Use tempo_flow_server tools for workflows

5. **MITRE ATT&CK Analysis**: Analyze and visualize attack techniques
   - Use attack-layer server tools: get_technique_rollup, get_findings_by_technique, create_attack_layer
   - Generate ATT&CK Navigator layers for visualization

When a user mentions an ID or entity (finding, case, IP, hash, domain), ALWAYS use the appropriate MCP tool to retrieve it first. Never try to access these as files - they are stored in databases and accessed via MCP tools.
</available_mcp_tools>

<recognizing_security_entities>
Common patterns you should recognize and how to handle them:

- Finding IDs: "f-YYYYMMDD-XXXXXXXX" → Use deeptempo-findings_get_finding tool
- Case IDs: "case-YYYYMMDD-XXXXXXXX" → Use deeptempo-findings_get_case tool  
- IP addresses: X.X.X.X → Consider using IP geolocation or threat intel tools
- Domain names: example.com → Consider using URL analysis or threat intel tools
- File hashes: MD5/SHA1/SHA256 → Consider using malware analysis tools
- URLs: http(s)://... → Consider using URL analysis tools

IMPORTANT: When a user says "analyze [ID]", "check [ID]", "investigate [ID]", etc., your FIRST action should ALWAYS be to use the appropriate MCP tool to fetch that entity's data.
</recognizing_security_entities>

<security_analysis_workflow>
When analyzing security findings and cases:
1. **Retrieve**: Use MCP tools to fetch the finding/case data first
2. **Understand**: Parse the severity, data source, MITRE techniques, and context
3. **Correlate**: Look for related findings or patterns using similarity/correlation tools
4. **Enrich**: Use threat intelligence tools to add external context
5. **Analyze**: Provide clear assessment of the threat, impact, and recommended actions
6. **Act**: Be thorough but efficient - prioritize actionable insights
</security_analysis_workflow>

<case_management_capabilities>
You have comprehensive tools to manage ALL aspects of cases during investigations:

**1. FINDINGS MANAGEMENT**
- Add single/multiple findings to cases
- Remove findings from cases
- Track why findings were added

**2. ACTIVITIES & NOTES**
- Log investigation activities automatically
- Activity types: note, action_taken, investigation_step, analysis, communication, task_update
- Track all investigation actions

**3. TIMELINE & KILL CHAIN**
- Build chronological attack timelines
- Tag MITRE ATT&CK techniques
- Document attack progression stages
- Create structured kill chain cases

**4. COMMENTS & COLLABORATION**
- Add comments to cases (threaded discussions)
- Get all comments for review
- Support team collaboration on investigations
- Use: `add_case_comment(case_id, author, content)`

**5. EVIDENCE MANAGEMENT**
- Add evidence/artifacts with chain of custody
- Types: file, log, network_capture, memory_dump, screenshot
- Track who collected what and when
- Use: `add_case_evidence(case_id, evidence_type, name, collected_by, ...)`

**6. IOCs (Indicators of Compromise)**
- Add IOCs: IP addresses, domains, hashes, URLs, emails, file names
- Bulk add multiple IOCs at once
- Track threat level and confidence
- Get all IOCs for a case
- Use: `add_case_ioc(case_id, ioc_type, value, threat_level, ...)` or `bulk_add_iocs(case_id, iocs)`

**7. TASK MANAGEMENT**
- Create investigation tasks
- Assign tasks to team members
- Update task status (pending, in_progress, completed, cancelled)
- Track task completion
- Use: `add_case_task(case_id, title, ...)` and `update_case_task(task_id, status, ...)`

**8. CASE RELATIONSHIPS**
- Link related cases (duplicate, related, parent, child, blocks, blocked_by)
- Track case relationships
- Build case hierarchies
- Use: `link_related_cases(case_id, related_case_id, relationship_type, created_by, ...)`

**9. ESCALATIONS**
- Escalate cases to higher tiers or management
- Track escalation reasons and urgency
- Auto-update priority for critical escalations
- Use: `escalate_case(case_id, escalated_from, escalated_to, reason, urgency_level)`

**10. CASE CLOSURE**
- Properly close cases with full metadata
- Categories: resolved, false_positive, duplicate, unable_to_resolve
- Document root cause, lessons learned, recommendations
- Include executive summary
- Use: `close_case(case_id, closure_category, closed_by, root_cause, lessons_learned, ...)`

**11. RESOLUTION STEPS**
- Document remediation actions taken
- Track results of each action
- Build comprehensive resolution timeline

**WHEN THE USER SAYS:**
- "Add this to case-123" → Add finding automatically
- "Comment that this is suspicious" → Add comment to case
- "Log evidence from the firewall" → Add evidence to case
- "Add IOC 192.168.1.5 as malicious IP" → Add IOC with threat level
- "Create a task to analyze the malware" → Add task to case
- "This is related to case-456" → Link cases as related
- "Escalate this to the SOC manager" → Escalate case
- "Close this case - it was a false positive" → Close case with category
- "Add these 5 IPs as IOCs" → Bulk add IOCs

**BE COMPREHENSIVE AND PROACTIVE:**
- Add IOCs as you discover them
- Create tasks for follow-up work
- Add evidence as it's collected
- Link related cases when patterns emerge
- Escalate when appropriate
- Document everything in comments and activities
- Close cases properly with full metadata

**NO PERMISSION NEEDED**: Just do it and confirm what you did. The user expects you to manage cases completely.
</case_management_capabilities>

Your goal is to help SOC analysts work more efficiently by leveraging all available tools and integrations to provide comprehensive, accurate, and actionable security analysis. When investigating, you should automatically build out cases with all relevant findings, activities, timeline entries, and MITRE mappings as the investigation progresses."""

    def _load_api_key(self) -> bool:
        """Load API key from secure storage.

        When ``provider_api_key_ref`` is set (GH #88), the secret named by
        that ref is tried first. This lets LLMRouter instantiate ClaudeService
        with a non-default Anthropic provider row. Falls back to the legacy
        CLAUDE_API_KEY / ANTHROPIC_API_KEY chain for backward compatibility.
        """
        try:
            # Use secrets manager with fallback to legacy names
            provider_key = (
                get_secret(self.provider_api_key_ref)
                if self.provider_api_key_ref
                else None
            )
            self.api_key = (
                provider_key
                or get_secret("CLAUDE_API_KEY")
                or get_secret("ANTHROPIC_API_KEY")
                or get_secret("claude_api_key")
                or get_secret("anthropic_api_key")
            )

            if self.api_key and ANTHROPIC_AVAILABLE:
                # Set longer timeout for operations that may take more than 10 minutes
                # Default is 600 seconds (10 min), we set to 1800 seconds (30 min)
                self.client = Anthropic(api_key=self.api_key, timeout=1800.0)
                self.async_client = AsyncAnthropic(api_key=self.api_key, timeout=1800.0)
                return True

            return False

        except Exception as e:
            logger.error(f"Error loading API key: {e}")
            return False

    def _load_backend_tools(self):
        """Load backend tools for Claude to use via function calling."""
        self.backend_tools = list(BACKEND_TOOLS)
        logger.info(f"Loaded {len(self.backend_tools)} backend tools")
        for tool in self.backend_tools:
            logger.debug(f"  - {tool['name']}: {tool['description'][:60]}...")

    async def _execute_backend_tool(self, tool_name: str, tool_input: dict):
        """Execute a single backend tool by name. Used by the daemon agent runner."""
        from services.database_data_service import DatabaseDataService

        data_service = DatabaseDataService()

        if tool_name == "list_findings":
            limit = tool_input.get("limit", 20)
            offset = tool_input.get("offset", 0)
            severity = tool_input.get("severity")
            data_source = tool_input.get("data_source")
            status = tool_input.get("status")
            total = data_service.count_findings(
                severity=severity, data_source=data_source, status=status
            )
            findings = data_service.get_findings(
                limit=limit,
                offset=offset,
                severity=severity,
                data_source=data_source,
                status=status,
                sort_by=tool_input.get("sort_by", "timestamp"),
                sort_order=tool_input.get("sort_order", "desc"),
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
                "total": total,
                "offset": offset,
                "limit": limit,
                "has_more": (offset + limit) < total,
                "findings": compact,
            }

        elif tool_name == "search_findings":
            query = tool_input.get("query", "")
            limit = tool_input.get("limit", 20)
            offset = tool_input.get("offset", 0)
            severity = tool_input.get("severity")
            data_source = tool_input.get("data_source")
            status = tool_input.get("status")
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
                sort_by=tool_input.get("sort_by", "anomaly_score"),
                sort_order=tool_input.get("sort_order", "desc"),
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

        elif tool_name == "get_findings_stats":
            findings = data_service.get_findings(limit=10000)
            severity_counts: dict = {}
            data_source_counts: dict = {}
            status_counts: dict = {}
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

        elif tool_name == "get_finding":
            return data_service.get_finding(**tool_input)

        elif tool_name == "nearest_neighbors":
            return data_service.get_nearest_neighbors(**tool_input)

        elif tool_name == "list_cases":
            limit = tool_input.get("limit", 50)
            status = tool_input.get("status")
            severity = tool_input.get("severity")
            cases = data_service.get_cases(limit=limit * 2)
            if status:
                cases = [c for c in cases if c.get("status") == status]
            if severity:
                cases = [c for c in cases if c.get("severity") == severity]
            return cases[:limit]

        elif tool_name == "get_case":
            return data_service.get_case(**tool_input)

        elif tool_name == "create_case":
            return data_service.create_case(
                title=tool_input["title"],
                finding_ids=tool_input.get("finding_ids", []),
                priority=tool_input.get("severity", "medium"),
                description=tool_input.get("description", ""),
            )

        elif tool_name == "add_finding_to_case":
            return data_service.add_finding_to_case(
                case_id=tool_input["case_id"],
                finding_id=tool_input["finding_id"],
            )

        elif tool_name == "update_case":
            case_id = tool_input.pop("case_id")
            success = data_service.update_case(case_id, **tool_input)
            return {"success": success, "case_id": case_id}

        elif tool_name == "add_resolution_step":
            case = data_service.get_case(tool_input["case_id"])
            if not case:
                return {"error": f"Case {tool_input['case_id']} not found"}
            resolution_steps = case.get("resolution_steps", [])
            from datetime import datetime as _dt

            resolution_steps.append(
                {
                    "timestamp": _dt.utcnow().isoformat() + "Z",
                    "description": tool_input["description"],
                    "action_taken": tool_input["action_taken"],
                    "result": tool_input.get("result"),
                }
            )
            data_service.update_case(
                tool_input["case_id"], resolution_steps=resolution_steps
            )
            return {
                "success": True,
                "case_id": tool_input["case_id"],
                "total_steps": len(resolution_steps),
            }

        elif tool_name in [
            "analyze_coverage",
            "search_detections",
            "identify_gaps",
            "get_coverage_stats",
            "get_detection_count",
        ]:
            security_tools = get_security_detection_tools()
            import asyncio

            loop = asyncio.new_event_loop()
            try:
                handler = getattr(security_tools, tool_name)
                return loop.run_until_complete(handler(**tool_input))
            finally:
                loop.close()

        elif tool_name in ["get_attack_layer", "get_technique_rollup"]:
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
            else:
                min_conf = tool_input.get("min_confidence", 0.0) if tool_input else 0.0
                findings = data_service.get_findings(limit=1000)
                counts: dict = {}
                severities: dict = {}
                for f in findings:
                    for tech in f.get("predicted_techniques", []) or []:
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

        elif tool_name in [
            "list_pending_approvals",
            "get_approval_action",
            "approve_action",
            "reject_action",
            "get_approval_stats",
        ]:
            from services.approval_service import ApprovalService

            approval_service = ApprovalService()
            from dataclasses import asdict

            if tool_name == "list_pending_approvals":
                actions = approval_service.list_pending_approvals()
                return [asdict(a) for a in actions[: tool_input.get("limit", 50)]]
            elif tool_name == "get_approval_action":
                action = approval_service.get_action(tool_input["action_id"])
                return asdict(action) if action else {"error": "Action not found"}
            elif tool_name == "approve_action":
                action = approval_service.approve_action(**tool_input)
                return asdict(action) if action else {"error": "Cannot approve"}
            elif tool_name == "reject_action":
                action = approval_service.reject_action(**tool_input)
                return asdict(action) if action else {"error": "Cannot reject"}
            elif tool_name == "get_approval_stats":
                return approval_service.get_stats()

        try:
            mcp_result = await self._execute_mcp_tool(tool_name, tool_input)
            logger.info(f"✅ Executed MCP tool: {tool_name}")
            return {"result": mcp_result}
        except Exception as e:
            logger.warning(f"Unknown tool: {tool_name}")
            return {"error": f"Unknown tool: {tool_name}"}

    def _load_mcp_tools(self):
        """Load MCP tools for Claude to use from persistent cache."""
        # Clear existing tools to prevent duplicates
        self.mcp_tools = []

        try:
            # Compute cache file path relative to project root
            cache_file = Path(__file__).parent.parent / "data" / "mcp_tools_cache.json"

            tools_dict = {}

            # First, try to load from persistent cache file (works in all contexts)
            if cache_file.exists():
                try:
                    with open(cache_file, "r") as f:
                        tools_dict = json.load(f)
                    logger.info(
                        f"✓ Loaded {sum(len(v) for v in tools_dict.values())} MCP tools from cache file"
                    )
                except Exception as e:
                    logger.warning(f"Could not load tools from cache file: {e}")
                    tools_dict = {}

            # If cache file didn't yield tools, fall back to in-memory cache
            if not tools_dict:
                from services.mcp_client import get_mcp_client

                mcp_client = get_mcp_client()
                if mcp_client and mcp_client.tools_cache:
                    tools_dict = mcp_client.tools_cache
                    logger.info("✓ Using in-memory MCP tools cache")
                else:
                    logger.warning("No MCP tools available - cache not yet populated")
                    return

            # Track tool names to prevent duplicates
            seen_tool_names = set()

            # Flatten tools from all servers with server prefix
            for server_name, server_tools in tools_dict.items():
                for tool in server_tools:
                    # Format for Claude API with server prefix
                    tool_name = f"{server_name}_{tool['name']}"

                    # Skip if we've already seen this tool name
                    if tool_name in seen_tool_names:
                        logger.warning(f"Skipping duplicate tool: {tool_name}")
                        continue
                    seen_tool_names.add(tool_name)

                    # Get input schema - handle both dict and object formats
                    input_schema = tool.get("inputSchema", {})
                    if hasattr(input_schema, "model_dump"):
                        input_schema = input_schema.model_dump()
                    elif not isinstance(input_schema, dict):
                        input_schema = dict(input_schema) if input_schema else {}

                    # Ensure input_schema has required structure
                    if not input_schema or "type" not in input_schema:
                        input_schema = {
                            "type": "object",
                            "properties": {},
                            "required": [],
                        }

                    claude_tool = {
                        "name": tool_name,
                        "description": f"[{server_name}] {tool.get('description', '')}",
                        "input_schema": input_schema,
                    }
                    self.mcp_tools.append(claude_tool)

            if self.mcp_tools:
                tool_names = [t["name"] for t in self.mcp_tools]
                logger.info(
                    f"✓ Loaded {len(self.mcp_tools)} MCP tools from {len(tools_dict)} servers"
                )
                logger.debug(f"Available tools: {', '.join(tool_names)}")

                # Populate the MCP registry for dynamic tool discovery
                self._populate_mcp_registry(tools_dict)
            else:
                logger.warning(
                    "No MCP tools were loaded. Check that MCP servers are configured and running."
                )
        except Exception as e:
            logger.warning(f"Could not load MCP tools: {e}")
            self.mcp_tools = []

    def _populate_mcp_registry(self, tools_dict: Dict):
        """Populate the MCP registry with discovered tools for dynamic tool discovery."""
        try:
            from services.mcp_client import get_mcp_client
            from services.mcp_registry import get_mcp_registry

            registry = get_mcp_registry()
            mcp_client = get_mcp_client()

            for server_name, server_tools in tools_dict.items():
                # Build tool list
                tools = []
                for tool in server_tools:
                    input_schema = tool.get("inputSchema", {})
                    if hasattr(input_schema, "model_dump"):
                        input_schema = input_schema.model_dump()
                    elif not isinstance(input_schema, dict):
                        input_schema = dict(input_schema) if input_schema else {}

                    tools.append(
                        {
                            "name": tool.get("name", "unknown"),
                            "description": tool.get("description", ""),
                            "inputSchema": input_schema,
                        }
                    )

                # Get server config from MCPService
                config = {}
                if mcp_client and mcp_client.mcp_service:
                    mcp_service = mcp_client.mcp_service
                    if server_name in mcp_service.servers:
                        server = mcp_service.servers[server_name]
                        config = {
                            "command": server.command,
                            "args": server.args,
                            "env": server.env,
                        }

                registry.register_server(server_name, config, tools)

            logger.info(f"MCP registry populated with {len(tools_dict)} servers")
        except Exception as e:
            logger.debug(f"Could not populate MCP registry: {e}")

    def set_api_key(self, api_key: str, save: bool = True) -> bool:
        """
        Set the API key.

        Args:
            api_key: The Anthropic API key.
            save: Whether to save the key securely.

        Returns:
            True if successful, False otherwise.
        """
        if not api_key or not api_key.strip():
            return False

        self.api_key = api_key.strip()

        if not ANTHROPIC_AVAILABLE:
            logger.warning(
                "Anthropic package not available. Install with: pip install anthropic"
            )
            return False

        try:
            # Set longer timeout for operations that may take more than 10 minutes
            # Default is 600 seconds (10 min), we set to 1800 seconds (30 min)
            self.client = Anthropic(api_key=self.api_key, timeout=1800.0)
            self.async_client = AsyncAnthropic(api_key=self.api_key, timeout=1800.0)

            if save:
                # Save using secrets manager
                set_secret("CLAUDE_API_KEY", self.api_key)

            return True

        except Exception as e:
            logger.error(f"Error setting API key: {e}")
            return False

    def has_api_key(self) -> bool:
        """Check if API key is configured."""
        return self.api_key is not None and self.client is not None

    def _extract_content_blocks(
        self, content, include_thinking: bool = False
    ) -> Union[str, List[Dict]]:
        """
        Extract content blocks from Claude's response.

        Args:
            content: Response content blocks
            include_thinking: Whether to include thinking blocks in the output

        Returns:
            String (if only one text block) or list of content blocks
        """
        blocks = []

        logger.debug(
            f"🔍 Extracting content blocks - include_thinking: {include_thinking}, content_len: {len(content) if content else 0}"
        )

        for i, content_block in enumerate(content):
            if hasattr(content_block, "type"):
                block_type = content_block.type

                if block_type == "text" and hasattr(content_block, "text"):
                    text_len = len(content_block.text)
                    logger.debug(f"  Block {i}: text ({text_len} chars)")
                    blocks.append({"type": "text", "text": content_block.text})
                elif (
                    block_type == "thinking"
                    and include_thinking
                    and hasattr(content_block, "thinking")
                ):
                    thinking_len = len(content_block.thinking)
                    logger.info(f"  💭 Block {i}: thinking ({thinking_len} chars)")
                    blocks.append({"type": "thinking", "text": content_block.thinking})
                elif block_type == "thinking" and not include_thinking:
                    logger.debug(
                        f"  Block {i}: thinking (skipped - include_thinking=False)"
                    )

        logger.debug(f"📦 Extracted {len(blocks)} blocks")

        # If only one text block, return as string for backward compatibility
        if len(blocks) == 1 and blocks[0]["type"] == "text":
            logger.debug("   Returning single text block as string")
            return blocks[0]["text"]

        # If we have multiple blocks or thinking blocks, return as list
        if blocks:
            logger.debug("   Returning multiple blocks as list")
            return blocks

        logger.warning("   No blocks extracted!")
        return None

    # ------------------------------------------------------------------
    # Reasoning-trace persistence (GH #79)
    # ------------------------------------------------------------------

    @staticmethod
    def _serialize_response_blocks(content) -> List[Dict]:
        """Convert Anthropic SDK content blocks to JSON-safe dicts."""
        if not content:
            return []
        out = []
        for block in content:
            btype = (
                getattr(block, "type", None)
                if not isinstance(block, dict)
                else block.get("type")
            )
            if btype == "text":
                text = (
                    block.text if not isinstance(block, dict) else block.get("text", "")
                )
                out.append({"type": "text", "text": text})
            elif btype == "thinking":
                text = (
                    block.thinking
                    if not isinstance(block, dict)
                    else block.get("text") or block.get("thinking", "")
                )
                out.append({"type": "thinking", "text": text})
            elif btype == "tool_use":
                out.append(
                    {
                        "type": "tool_use",
                        "id": (
                            getattr(block, "id", None)
                            if not isinstance(block, dict)
                            else block.get("id")
                        ),
                        "name": (
                            getattr(block, "name", None)
                            if not isinstance(block, dict)
                            else block.get("name")
                        ),
                        "input": (
                            getattr(block, "input", None)
                            if not isinstance(block, dict)
                            else block.get("input")
                        ),
                    }
                )
            elif btype == "tool_result":
                out.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": (
                            getattr(block, "tool_use_id", None)
                            if not isinstance(block, dict)
                            else block.get("tool_use_id")
                        ),
                        "content": (
                            getattr(block, "content", None)
                            if not isinstance(block, dict)
                            else block.get("content")
                        ),
                        "is_error": (
                            getattr(block, "is_error", False)
                            if not isinstance(block, dict)
                            else block.get("is_error", False)
                        ),
                    }
                )
        return out

    @staticmethod
    def _sanitize_messages_for_log(messages: List[Dict]) -> List[Dict]:
        """Strip heavy image base64 payloads from messages before logging."""
        if not messages:
            return []
        sanitized = []
        for msg in messages:
            role = msg.get("role")
            content = msg.get("content")
            if isinstance(content, str):
                sanitized.append({"role": role, "content": content})
                continue
            if not isinstance(content, list):
                sanitized.append({"role": role, "content": content})
                continue
            clean_blocks = []
            for block in content:
                bdict = (
                    block
                    if isinstance(block, dict)
                    else {"type": getattr(block, "type", "unknown")}
                )
                btype = bdict.get("type")
                if btype == "image":
                    clean_blocks.append(
                        {"type": "image", "source": {"type": "redacted"}}
                    )
                else:
                    clean_blocks.append(
                        ClaudeService._serialize_response_blocks([block])[0]
                        if not isinstance(block, dict)
                        else block
                    )
            sanitized.append({"role": role, "content": clean_blocks})
        return sanitized

    @staticmethod
    def _extract_prior_tool_results(messages: List[Dict]) -> List[Dict]:
        """Return tool_result blocks from the most recent user message, if any.

        Used to capture the "input" context for an iteration that consumed
        tool results from the prior iteration's tool calls.
        """
        if not messages:
            return []
        for msg in reversed(messages):
            if msg.get("role") != "user":
                continue
            content = msg.get("content")
            if isinstance(content, list):
                results = [
                    b
                    for b in content
                    if (isinstance(b, dict) and b.get("type") == "tool_result")
                    or (
                        not isinstance(b, dict)
                        and getattr(b, "type", None) == "tool_result"
                    )
                ]
                if results:
                    return ClaudeService._serialize_response_blocks(results)
            return []
        return []

    def _persist_interaction(
        self,
        *,
        session_id: Optional[str],
        agent_id: Optional[str],
        investigation_id: Optional[str],
        model: str,
        system_prompt: Optional[str],
        request_messages: List[Dict],
        response_content: Optional[List[Dict]],
        thinking_enabled: bool,
        thinking_budget: Optional[int],
        stop_reason: Optional[str],
        input_tokens: int,
        output_tokens: int,
        cache_read_tokens: int = 0,
        cache_creation_tokens: int = 0,
        duration_ms: int = 0,
        error: Optional[str] = None,
    ) -> None:
        """Fire-and-forget insert of an LLMInteractionLog row.

        Runs in the calling thread; failures are logged but never re-raised
        so persistence can never break the request path.
        """
        try:
            from database.connection import get_db_manager
            from database.models import LLMInteractionLog

            blocks = self._serialize_response_blocks(response_content or [])
            thinking_text = "\n\n".join(
                b["text"] for b in blocks if b["type"] == "thinking"
            )
            response_text = "\n\n".join(
                b["text"] for b in blocks if b["type"] == "text"
            )
            tool_calls = [b for b in blocks if b["type"] == "tool_use"]
            tool_results_in = self._extract_prior_tool_results(request_messages)

            try:
                from daemon.agent_runner import SONNET_INPUT_COST, SONNET_OUTPUT_COST

                cost_usd = (input_tokens * SONNET_INPUT_COST) + (
                    output_tokens * SONNET_OUTPUT_COST
                )
            except Exception:
                cost_usd = 0.0

            row = LLMInteractionLog(
                interaction_id=str(uuid.uuid4()),
                session_id=session_id,
                agent_id=agent_id,
                investigation_id=investigation_id,
                model=model,
                system_prompt=system_prompt,
                request_messages=self._sanitize_messages_for_log(request_messages),
                thinking_enabled=bool(thinking_enabled),
                thinking_budget=thinking_budget,
                thinking_content=thinking_text or None,
                response_content=response_text or None,
                tool_calls=tool_calls,
                tool_results=tool_results_in,
                stop_reason=stop_reason,
                input_tokens=int(input_tokens or 0),
                output_tokens=int(output_tokens or 0),
                cache_read_tokens=int(cache_read_tokens or 0),
                cache_creation_tokens=int(cache_creation_tokens or 0),
                cost_usd=float(cost_usd or 0.0),
                duration_ms=int(duration_ms or 0),
                error=error,
            )
            db_manager = get_db_manager()
            with db_manager.session_scope() as session:
                session.add(row)
        except Exception as exc:
            logger.warning(f"LLMInteractionLog persist failed (non-fatal): {exc}")

    def _strip_thinking_blocks(self, messages: List[Dict]) -> List[Dict]:
        """
        Strip thinking blocks from assistant messages when thinking is disabled.

        This prevents errors when conversation history contains thinking blocks
        but thinking mode is disabled for the current request.
        """
        cleaned_messages = []
        for msg in messages:
            if msg.get("role") == "assistant":
                content = msg.get("content")
                if isinstance(content, list):
                    # Filter out thinking blocks
                    cleaned_content = []
                    for block in content:
                        if isinstance(block, dict):
                            if block.get("type") != "thinking":
                                cleaned_content.append(block)
                        elif hasattr(block, "type"):
                            if block.type != "thinking":
                                # Convert to dict format
                                if block.type == "text" and hasattr(block, "text"):
                                    cleaned_content.append(
                                        {"type": "text", "text": block.text}
                                    )
                                elif block.type == "tool_use":
                                    cleaned_content.append(
                                        {
                                            "type": "tool_use",
                                            "id": getattr(block, "id", ""),
                                            "name": getattr(block, "name", ""),
                                            "input": getattr(block, "input", {}),
                                        }
                                    )

                    # Only include message if it has non-thinking content
                    if cleaned_content:
                        cleaned_messages.append(
                            {"role": "assistant", "content": cleaned_content}
                        )
                elif isinstance(content, str):
                    # String content doesn't contain thinking blocks
                    cleaned_messages.append(msg)
            else:
                # Non-assistant messages pass through unchanged
                cleaned_messages.append(msg)

        return cleaned_messages

    def _estimate_tokens(self, content: any) -> int:
        """
        Estimate token count for content (rough: ~4 chars per token).

        Args:
            content: String, list of content blocks, or list of messages

        Returns:
            Estimated token count
        """
        if isinstance(content, str):
            return len(content) // 4
        elif isinstance(content, list):
            total = 0
            for item in content:
                if isinstance(item, str):
                    total += len(item) // 4
                elif isinstance(item, dict):
                    # Message or content block
                    if "content" in item:
                        total += self._estimate_tokens(item["content"])
                    if "text" in item:
                        total += len(item["text"]) // 4
                    if "input" in item:
                        total += len(json.dumps(item["input"])) // 4
                elif hasattr(item, "text"):
                    total += len(getattr(item, "text", "")) // 4
            return total
        return 0

    def _needs_context_reduction(
        self,
        messages: List[Dict],
        system_prompt: Optional[str] = None,
        max_context_tokens: int = 180000,
    ) -> tuple:
        """
        Check if messages exceed the context window and calculate budget.

        Returns:
            Tuple of (needs_reduction: bool, total_tokens: int, available_tokens: int)
        """
        system_tokens = self._estimate_tokens(system_prompt) if system_prompt else 0

        tool_tokens = 0
        if self.use_backend_tools and self.backend_tools:
            tool_tokens += self._estimate_tokens(json.dumps(self.backend_tools))
        if self.use_mcp_tools and self.mcp_tools:
            tool_tokens += self._estimate_tokens(json.dumps(self.mcp_tools))

        available_tokens = max_context_tokens - system_tokens - tool_tokens
        if available_tokens <= 0:
            available_tokens = 50000

        total_tokens = sum(
            self._estimate_tokens(msg.get("content", "")) for msg in messages
        )
        return total_tokens > available_tokens, total_tokens, available_tokens

    def _split_messages_for_summary(
        self, messages: List[Dict], available_tokens: int
    ) -> tuple:
        """
        Split messages into (older_to_summarize, recent_to_keep).

        Keeps enough recent messages to fit in ~60% of the budget,
        leaving room for the summary of older messages.

        Returns:
            Tuple of (messages_to_summarize, messages_to_keep)
        """
        if not messages:
            return [], []

        # Reserve budget: 60% for recent messages, 40% for summary of old messages
        recent_budget = int(available_tokens * 0.6)

        # Always keep the last message (current user input)
        keep = []
        used = 0
        for msg in reversed(messages):
            msg_tokens = self._estimate_tokens(msg.get("content", ""))
            if used + msg_tokens > recent_budget and len(keep) >= 2:
                break
            keep.insert(0, msg)
            used += msg_tokens

        # Everything not in keep gets summarized
        keep_start_idx = len(messages) - len(keep)
        to_summarize = messages[:keep_start_idx]

        return to_summarize, keep

    def _format_messages_for_summary(self, messages: List[Dict]) -> str:
        """Convert messages to plain text for summarization."""
        parts = []
        for msg in messages:
            role = msg.get("role", "unknown").upper()
            content = msg.get("content", "")
            if isinstance(content, str):
                parts.append(f"{role}: {content}")
            elif isinstance(content, list):
                text_parts = []
                for block in content:
                    if isinstance(block, dict):
                        if block.get("type") == "text" and block.get("text"):
                            text_parts.append(block["text"])
                        elif block.get("type") == "thinking" and block.get("text"):
                            text_parts.append(f"[Thinking: {block['text'][:300]}...]")
                        elif block.get("type") == "image":
                            text_parts.append("[Image]")
                    elif hasattr(block, "type"):
                        if block.type == "text" and hasattr(block, "text"):
                            text_parts.append(block.text)
                if text_parts:
                    parts.append(f"{role}: {' '.join(text_parts)}")
        return "\n\n".join(parts)

    def _build_summary_prompt(self, conversation_text: str) -> str:
        """Build the prompt for summarizing a conversation."""
        # Cap input to ~100k tokens for the summarization call itself
        max_chars = 400000
        if len(conversation_text) > max_chars:
            conversation_text = (
                conversation_text[:max_chars]
                + "\n\n[... earlier messages truncated ...]"
            )

        return f"""Summarize the following conversation between a user and an AI security assistant.
You MUST preserve ALL of the following:
- Every finding ID (f-XXXXXXXX-XXXXXXXX), case ID (case-XXXXXXXX), and IOC mentioned
- All investigation decisions, conclusions, and action items
- Key analysis results and threat assessments
- Entity references (IPs, domains, hashes, hostnames, usernames)
- The current state of any ongoing investigation
- Any pending questions or next steps

Be thorough. This summary replaces the conversation history — anything you omit is lost.

CONVERSATION ({len(conversation_text)} chars):
{conversation_text}

Provide a structured summary preserving all critical context."""

    def _summarize_messages_sync(
        self, messages: List[Dict], model: str = "claude-sonnet-4-20250514"
    ) -> Optional[str]:
        """
        Synchronously summarize a list of messages using a lightweight Claude call.

        Returns:
            Summary text, or None if summarization fails
        """
        if not messages or not self.has_api_key():
            return None

        conversation_text = self._format_messages_for_summary(messages)
        if not conversation_text.strip():
            return None

        prompt = self._build_summary_prompt(conversation_text)

        try:
            logger.info(
                f"📝 Auto-summarizing {len(messages)} messages (~{len(conversation_text)} chars)..."
            )
            response = self.client.messages.create(
                model=model,
                max_tokens=4096,
                system="You are a precise conversation summarizer for a security operations platform. Preserve all entity IDs, findings, and investigation context.",
                messages=[{"role": "user", "content": prompt}],
            )

            if response.content:
                for block in response.content:
                    if hasattr(block, "text") and block.text:
                        logger.info(
                            f"✅ Summarized {len(messages)} messages into ~{len(block.text)} chars"
                        )
                        return block.text
            return None
        except Exception as e:
            logger.error(f"❌ Auto-summarization failed: {e}")
            return None

    async def _summarize_messages_async(
        self, messages: List[Dict], model: str = "claude-sonnet-4-20250514"
    ) -> Optional[str]:
        """
        Asynchronously summarize a list of messages using a lightweight Claude call.

        Returns:
            Summary text, or None if summarization fails
        """
        if not messages or not self.has_api_key():
            return None

        conversation_text = self._format_messages_for_summary(messages)
        if not conversation_text.strip():
            return None

        prompt = self._build_summary_prompt(conversation_text)

        try:
            logger.info(
                f"📝 Auto-summarizing {len(messages)} messages (~{len(conversation_text)} chars) [async]..."
            )
            response = await self.async_client.messages.create(
                model=model,
                max_tokens=4096,
                system="You are a precise conversation summarizer for a security operations platform. Preserve all entity IDs, findings, and investigation context.",
                messages=[{"role": "user", "content": prompt}],
            )

            if response.content:
                for block in response.content:
                    if hasattr(block, "text") and block.text:
                        logger.info(
                            f"✅ Summarized {len(messages)} messages into ~{len(block.text)} chars"
                        )
                        return block.text
            return None
        except Exception as e:
            logger.error(f"❌ Auto-summarization failed: {e}")
            return None

    def _prepare_context_sync(
        self,
        messages: List[Dict],
        system_prompt: Optional[str] = None,
        model: str = "claude-sonnet-4-20250514",
        max_context_tokens: int = 180000,
    ) -> tuple:
        """
        Prepare messages for API call, auto-summarizing if context is too long.
        Synchronous version for chat().

        Returns:
            Tuple of (prepared_messages, summarized_count) where summarized_count
            is the number of messages that were summarized (0 if none).
        """
        needs_reduction, total_tokens, available_tokens = self._needs_context_reduction(
            messages, system_prompt, max_context_tokens
        )

        if not needs_reduction:
            return messages, 0

        logger.warning(
            f"⚠️ Context too long (~{total_tokens} tokens > {available_tokens} available), auto-summarizing..."
        )

        to_summarize, to_keep = self._split_messages_for_summary(
            messages, available_tokens
        )

        if not to_summarize:
            return messages, 0

        # Archive overflow messages to MemPalace before summarizing (lossless preservation)
        def _archive_overflow():
            sessions_dir = self._get_mempalace_sessions_dir()
            if not sessions_dir:
                return
            try:
                import time

                overflow_dir = sessions_dir.parent / "overflow"
                overflow_dir.mkdir(parents=True, exist_ok=True)
                dest = overflow_dir / f"overflow_{int(time.time())}.json"
                dest.write_text(
                    json.dumps({"messages": to_summarize, "reason": "context_overflow"})
                )
            except Exception:
                pass

        threading.Thread(target=_archive_overflow, daemon=True).start()

        summary = self._summarize_messages_sync(to_summarize, model=model)

        if summary:
            summary_msg = {
                "role": "user",
                "content": f"[CONVERSATION CONTEXT - Auto-summary of {len(to_summarize)} earlier messages]\n\n{summary}\n\n[END OF SUMMARY - The conversation continues below]",
            }
            # Ensure alternating roles: summary is "user", so next must be "assistant"
            prepared = [summary_msg]
            if to_keep and to_keep[0].get("role") == "user":
                prepared.append(
                    {
                        "role": "assistant",
                        "content": "Understood, I have the context from our previous conversation. Let me continue helping you.",
                    }
                )
            prepared.extend(to_keep)

            new_tokens = sum(
                self._estimate_tokens(msg.get("content", "")) for msg in prepared
            )
            logger.info(
                f"✅ Context reduced: {total_tokens} → ~{new_tokens} tokens ({len(to_summarize)} messages summarized, {len(to_keep)} kept)"
            )
            return prepared, len(to_summarize)
        else:
            # Summarization failed - fall back to keeping only recent messages
            logger.warning(
                "⚠️ Summarization failed, falling back to recent messages only"
            )
            return to_keep, len(to_summarize)

    async def _prepare_context_async(
        self,
        messages: List[Dict],
        system_prompt: Optional[str] = None,
        model: str = "claude-sonnet-4-20250514",
        max_context_tokens: int = 180000,
    ) -> tuple:
        """
        Prepare messages for API call, auto-summarizing if context is too long.
        Async version for chat_stream().

        Returns:
            Tuple of (prepared_messages, summarized_count)
        """
        needs_reduction, total_tokens, available_tokens = self._needs_context_reduction(
            messages, system_prompt, max_context_tokens
        )

        if not needs_reduction:
            return messages, 0

        logger.warning(
            f"⚠️ Context too long (~{total_tokens} tokens > {available_tokens} available), auto-summarizing..."
        )

        to_summarize, to_keep = self._split_messages_for_summary(
            messages, available_tokens
        )

        if not to_summarize:
            return messages, 0

        # Archive overflow messages to MemPalace before summarizing (lossless preservation)
        def _archive_overflow_async():
            sessions_dir = self._get_mempalace_sessions_dir()
            if not sessions_dir:
                return
            try:
                import time

                overflow_dir = sessions_dir.parent / "overflow"
                overflow_dir.mkdir(parents=True, exist_ok=True)
                dest = overflow_dir / f"overflow_{int(time.time())}.json"
                dest.write_text(
                    json.dumps({"messages": to_summarize, "reason": "context_overflow"})
                )
            except Exception:
                pass

        threading.Thread(target=_archive_overflow_async, daemon=True).start()

        summary = await self._summarize_messages_async(to_summarize, model=model)

        if summary:
            summary_msg = {
                "role": "user",
                "content": f"[CONVERSATION CONTEXT - Auto-summary of {len(to_summarize)} earlier messages]\n\n{summary}\n\n[END OF SUMMARY - The conversation continues below]",
            }
            prepared = [summary_msg]
            if to_keep and to_keep[0].get("role") == "user":
                prepared.append(
                    {
                        "role": "assistant",
                        "content": "Understood, I have the context from our previous conversation. Let me continue helping you.",
                    }
                )
            prepared.extend(to_keep)

            new_tokens = sum(
                self._estimate_tokens(msg.get("content", "")) for msg in prepared
            )
            logger.info(
                f"✅ Context reduced: {total_tokens} → ~{new_tokens} tokens ({len(to_summarize)} messages summarized, {len(to_keep)} kept)"
            )
            return prepared, len(to_summarize)
        else:
            logger.warning(
                "⚠️ Summarization failed, falling back to recent messages only"
            )
            return to_keep, len(to_summarize)

    MAX_TOOL_RESPONSE_TOKENS = 30000

    def _truncate_tool_response(self, content: str, max_tokens: int = None) -> str:
        """Truncate a tool response if it exceeds the token budget."""
        if max_tokens is None:
            max_tokens = self.MAX_TOOL_RESPONSE_TOKENS
        estimated_tokens = len(content) // 4
        if estimated_tokens <= max_tokens:
            return content
        truncated = content[: max_tokens * 4]
        return (
            truncated
            + f"\n\n[TRUNCATED: Response was ~{estimated_tokens} tokens, showing first ~{max_tokens}. "
            "Use more specific filters or pagination to see remaining data.]"
        )

    async def _process_backend_tool_use(self, content: List) -> List[Dict]:
        """Process tool use requests and call backend tools directly."""
        tool_results = []

        # Initialize tool instances lazily
        security_tools = None

        for item in content:
            # Handle both dict and object formats
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

            if item_type == "tool_use" and tool_name:
                try:
                    result = None

                    # Security detection tools
                    if tool_name in [
                        "analyze_coverage",
                        "search_detections",
                        "identify_gaps",
                        "get_coverage_stats",
                        "get_detection_count",
                    ]:
                        if security_tools is None:
                            security_tools = get_security_detection_tools()

                        if tool_name == "analyze_coverage":
                            result = await security_tools.analyze_coverage(**arguments)
                        elif tool_name == "search_detections":
                            result = await security_tools.search_detections(**arguments)
                        elif tool_name == "identify_gaps":
                            result = await security_tools.identify_gaps(**arguments)
                        elif tool_name == "get_coverage_stats":
                            result = await security_tools.get_coverage_stats(
                                **arguments
                            )
                        elif tool_name == "get_detection_count":
                            result = await security_tools.get_detection_count(
                                **arguments
                            )

                    # DeepTempo findings tools
                    elif tool_name in [
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
                    ]:
                        from services.database_data_service import DatabaseDataService

                        data_service = DatabaseDataService()

                        if tool_name == "list_findings":
                            limit = arguments.get("limit", 20)
                            offset = arguments.get("offset", 0)
                            severity = arguments.get("severity")
                            data_source = arguments.get("data_source")
                            status = arguments.get("status")

                            total = data_service.count_findings(
                                severity=severity,
                                data_source=data_source,
                                status=status,
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
                            compact = []
                            for f in findings:
                                compact.append(
                                    {
                                        "finding_id": f.get("finding_id"),
                                        "severity": f.get("severity"),
                                        "anomaly_score": float(
                                            f.get("anomaly_score") or 0
                                        ),
                                        "data_source": f.get("data_source"),
                                        "cluster_id": f.get("cluster_id"),
                                        "timestamp": f.get("timestamp"),
                                        "status": f.get("status"),
                                        "summary": (f.get("description") or "")[:200],
                                    }
                                )
                            result = {
                                "total": total,
                                "offset": offset,
                                "limit": limit,
                                "has_more": (offset + limit) < total,
                                "findings": compact,
                            }
                        elif tool_name == "search_findings":
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
                            compact = []
                            for f in findings:
                                compact.append(
                                    {
                                        "finding_id": f.get("finding_id"),
                                        "severity": f.get("severity"),
                                        "anomaly_score": float(
                                            f.get("anomaly_score") or 0
                                        ),
                                        "data_source": f.get("data_source"),
                                        "timestamp": f.get("timestamp"),
                                        "status": f.get("status"),
                                        "summary": (f.get("description") or "")[:200],
                                    }
                                )
                            result = {
                                "query": query,
                                "total": total,
                                "offset": offset,
                                "limit": limit,
                                "has_more": (offset + limit) < total,
                                "findings": compact,
                            }
                        elif tool_name == "get_findings_stats":
                            findings = data_service.get_findings(limit=10000)
                            severity_counts: dict = {}
                            data_source_counts: dict = {}
                            status_counts: dict = {}
                            for f in findings:
                                sev = f.get("severity") or "unknown"
                                severity_counts[sev] = severity_counts.get(sev, 0) + 1
                                ds = f.get("data_source") or "unknown"
                                data_source_counts[ds] = (
                                    data_source_counts.get(ds, 0) + 1
                                )
                                st = f.get("status") or "unknown"
                                status_counts[st] = status_counts.get(st, 0) + 1
                            result = {
                                "total_findings": len(findings),
                                "by_severity": severity_counts,
                                "by_data_source": data_source_counts,
                                "by_status": status_counts,
                            }
                        elif tool_name == "get_finding":
                            result = data_service.get_finding(**arguments)
                        elif tool_name == "nearest_neighbors":
                            result = data_service.get_nearest_neighbors(**arguments)
                        elif tool_name == "list_cases":
                            # Use get_cases and apply filters
                            limit = arguments.get("limit", 50)
                            status = arguments.get("status")
                            severity = arguments.get("severity")

                            cases = data_service.get_cases(limit=limit * 2)

                            # Apply filters
                            if status:
                                cases = [c for c in cases if c.get("status") == status]
                            if severity:
                                cases = [
                                    c for c in cases if c.get("severity") == severity
                                ]

                            result = cases[:limit]
                        elif tool_name == "get_case":
                            result = data_service.get_case(**arguments)
                        elif tool_name == "create_case":
                            result = data_service.create_case(
                                title=arguments["title"],
                                finding_ids=arguments.get("finding_ids", []),
                                priority=arguments.get("severity", "medium"),
                                description=arguments.get("description", ""),
                            )
                        elif tool_name == "add_finding_to_case":
                            result = data_service.add_finding_to_case(
                                case_id=arguments["case_id"],
                                finding_id=arguments["finding_id"],
                            )
                        elif tool_name == "update_case":
                            uc_args = dict(arguments)
                            uc_case_id = uc_args.pop("case_id")
                            success = data_service.update_case(uc_case_id, **uc_args)
                            result = {"success": success, "case_id": uc_case_id}
                        elif tool_name == "add_resolution_step":
                            case = data_service.get_case(arguments["case_id"])
                            if not case:
                                result = {
                                    "error": f"Case {arguments['case_id']} not found"
                                }
                            else:
                                from datetime import datetime as _dt

                                res_steps = case.get("resolution_steps", [])
                                res_steps.append(
                                    {
                                        "timestamp": _dt.utcnow().isoformat() + "Z",
                                        "description": arguments["description"],
                                        "action_taken": arguments["action_taken"],
                                        "result": arguments.get("result"),
                                    }
                                )
                                data_service.update_case(
                                    arguments["case_id"], resolution_steps=res_steps
                                )
                                result = {
                                    "success": True,
                                    "case_id": arguments["case_id"],
                                    "total_steps": len(res_steps),
                                }

                    # Attack layer tools
                    elif tool_name in ["get_attack_layer", "get_technique_rollup"]:
                        from services.database_data_service import DatabaseDataService

                        data_service = DatabaseDataService()

                        if tool_name == "get_attack_layer":
                            # Generate ATT&CK Navigator layer
                            layer = {
                                "name": "DeepTempo Findings",
                                "version": "4.5",
                                "domain": "enterprise-attack",
                                "description": "ATT&CK techniques from findings",
                                "techniques": [],
                            }
                            result = {"success": True, "layer": layer}
                        elif tool_name == "get_technique_rollup":
                            # Get technique statistics
                            min_conf = (
                                arguments.get("min_confidence", 0.0)
                                if arguments
                                else 0.0
                            )
                            findings = data_service.get_findings(limit=1000)

                            counts = {}
                            severities = {}
                            for f in findings:
                                predicted_techniques = (
                                    f.get("predicted_techniques", []) or []
                                )
                                for tech in predicted_techniques:
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
                                    sev = f.get("severity") or "medium"
                                    severities[tid][sev] = (
                                        severities[tid].get(sev, 0) + 1
                                    )

                            techniques = [
                                {
                                    "technique_id": t,
                                    "count": c,
                                    "severities": severities[t],
                                }
                                for t, c in counts.items()
                            ]
                            techniques.sort(key=lambda x: x["count"], reverse=True)
                            result = {
                                "success": True,
                                "total_techniques": len(techniques),
                                "techniques": techniques,
                            }

                    # Approval tools
                    elif tool_name in [
                        "list_pending_approvals",
                        "get_approval_action",
                        "approve_action",
                        "reject_action",
                        "get_approval_stats",
                    ]:
                        from services.approval_service import ApprovalService

                        approval_service = ApprovalService()

                        if tool_name == "list_pending_approvals":
                            actions = approval_service.list_pending_approvals()
                            # Convert to dict for JSON serialization
                            from dataclasses import asdict

                            result = [
                                asdict(action)
                                for action in actions[: arguments.get("limit", 50)]
                            ]
                        elif tool_name == "get_approval_action":
                            action = approval_service.get_action(arguments["action_id"])
                            if action:
                                from dataclasses import asdict

                                result = asdict(action)
                            else:
                                result = {"error": "Action not found"}
                        elif tool_name == "approve_action":
                            action = approval_service.approve_action(**arguments)
                            if action:
                                from dataclasses import asdict

                                result = asdict(action)
                            else:
                                result = {
                                    "error": "Action not found or cannot be approved"
                                }
                        elif tool_name == "reject_action":
                            action = approval_service.reject_action(**arguments)
                            if action:
                                from dataclasses import asdict

                                result = asdict(action)
                            else:
                                result = {
                                    "error": "Action not found or cannot be rejected"
                                }
                        elif tool_name == "get_approval_stats":
                            result = approval_service.get_stats()

                    else:
                        logger.warning(f"Unknown backend tool: {tool_name}")
                        result = {"error": f"Unknown tool: {tool_name}"}

                    # Format result for Claude API with size guard
                    if isinstance(result, dict) or isinstance(result, list):
                        content_str = json.dumps(result)
                    else:
                        content_str = str(result)
                    content_str = self._truncate_tool_response(content_str)

                    tool_results.append(
                        {
                            "type": "tool_result",
                            "tool_use_id": tool_id,
                            "content": [{"type": "text", "text": content_str}],
                        }
                    )

                    logger.info(f"✅ Executed backend tool: {tool_name}")

                except Exception as e:
                    logger.error(
                        f"Error calling backend tool {tool_name}: {e}", exc_info=True
                    )
                    tool_results.append(
                        {
                            "type": "tool_result",
                            "tool_use_id": tool_id,
                            "content": [{"type": "text", "text": f"Error: {str(e)}"}],
                        }
                    )

        return tool_results

    async def _process_tool_use(self, content: List) -> List[Dict]:
        """Process tool use requests and call MCP tools."""
        tool_results = []

        for item in content:
            # Handle both dict and object formats
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

            if item_type == "tool_use" and tool_name:
                # Extract server name from tool name (format: server_toolname)
                parts = tool_name.split("_", 1)
                if len(parts) == 2:
                    server_name, actual_tool_name = parts
                else:
                    # Try to find tool in any server by checking tool cache
                    server_name = None
                    actual_tool_name = tool_name
                    from services.mcp_client import get_mcp_client

                    mcp_client = get_mcp_client()
                    if mcp_client:
                        # Check which server has this tool
                        for srv_name, tools in mcp_client.tools_cache.items():
                            if any(t["name"] == tool_name for t in tools):
                                server_name = srv_name
                                break

                if server_name:
                    try:
                        from services.mcp_client import get_mcp_client

                        mcp_client = get_mcp_client()
                        if mcp_client:
                            # Call tool with 30 second timeout
                            result = await mcp_client.call_tool(
                                server_name, actual_tool_name, arguments, timeout=30.0
                            )

                            # Format result for Claude API with size guard
                            if isinstance(result, dict):
                                content = result.get(
                                    "content", [{"type": "text", "text": str(result)}]
                                )
                            else:
                                content = [{"type": "text", "text": str(result)}]
                            for block in content:
                                if (
                                    isinstance(block, dict)
                                    and block.get("type") == "text"
                                ):
                                    block["text"] = self._truncate_tool_response(
                                        block["text"]
                                    )

                            tool_results.append(
                                {
                                    "type": "tool_result",
                                    "tool_use_id": tool_id,
                                    "content": content,
                                }
                            )
                    except Exception as e:
                        logger.error(f"Error calling tool {tool_name}: {e}")
                        tool_results.append(
                            {
                                "type": "tool_result",
                                "tool_use_id": tool_id,
                                "content": [
                                    {"type": "text", "text": f"Error: {str(e)}"}
                                ],
                            }
                        )
                else:
                    logger.warning(f"Could not determine server for tool {tool_name}")

        return tool_results

    async def _process_mixed_tool_use(self, content: List) -> List[Dict]:
        """
        Routes each tool use request in `content` to the appropriate processor.
        Backend tools (by name match against self.backend_tools) are handled by
        _process_backend_tool_use(); all others are handled by _process_tool_use().

        Note: If a backend tool and an MCP tool share the same name, backend takes
        precedence since the backend name set is checked first.

        Args:
            content: List of tool use blocks from a Claude API response.

        Returns:
            Combined list of tool result dicts from both processors.
        """
        tool_results = []
        # Build a set of backend tool names for O(1) lookup
        backend_tool_names = {tool.get("name") for tool in (self.backend_tools or [])}

        for item in content:
            # Extract tool name (handle both dict and object formats)
            if isinstance(item, dict):
                tool_name = item.get("name")
            else:
                tool_name = getattr(item, "name", None)

            if not tool_name:
                continue

            # Route to appropriate processor based on tool name membership
            if tool_name in backend_tool_names:
                # Backend tool: dispatch to backend processor
                result = await self._process_backend_tool_use([item])
                tool_results.extend(result if result else [])
            else:
                # MCP tool (or unknown): dispatch to MCP processor
                result = await self._process_tool_use([item])
                tool_results.extend(result if result else [])

        return tool_results

    def chat(
        self,
        message: Union[str, List[Dict]],
        system_prompt: Optional[str] = None,
        context: Optional[List[Dict]] = None,
        model: str = "claude-sonnet-4-5-20250929",
        images: Optional[List[Dict]] = None,
        prefill: Optional[str] = None,
        max_tokens: int = 4096,
        enable_thinking: Optional[bool] = None,
        thinking_budget: Optional[int] = None,
        session_id: Optional[str] = None,
        agent_id: Optional[str] = None,
        investigation_id: Optional[str] = None,
    ) -> Optional[str]:
        """
        Send a chat message to Claude.

        Args:
            message: User message (string or list of content blocks for multimodal).
            system_prompt: Optional system prompt (uses default if None).
            context: Optional context messages (for conversation history).
            model: Claude model to use.
            images: Optional list of image content blocks (for vision).
            prefill: Optional prefill text to shape Claude's response.
            max_tokens: Maximum tokens for response (default: 4096).
            enable_thinking: Override thinking setting for this request.
            thinking_budget: Override thinking budget for this request.
            session_id: Chat/session identifier for reasoning-trace persistence.
            agent_id: Agent identifier (e.g. 'investigator') for reasoning-trace attribution.
            investigation_id: Investigation identifier if this call is part of one.

        Returns:
            Claude's response text or None if error.
        """
        if not self.has_api_key():
            raise ValueError(
                "API key not configured. Please set your Anthropic API key."
            )

        try:
            messages = []

            # Determine thinking settings first
            use_thinking = (
                enable_thinking if enable_thinking is not None else self.enable_thinking
            )

            logger.info(
                f"🤔 ClaudeService.chat() - Thinking: {use_thinking}, Budget: {thinking_budget or self.thinking_budget}, Model: {model}"
            )
            logger.info(f"📤 Sending to Claude API:")
            logger.info(f"   - Message type: {type(message).__name__}")
            if isinstance(message, str):
                logger.info(
                    f"   - Message preview: {message[:200]}..."
                    if len(message) > 200
                    else f"   - Message: {message}"
                )
            else:
                logger.info(f"   - Message blocks: {len(message)}")
            logger.info(f"   - Context messages: {len(context) if context else 0}")
            logger.info(f"   - Max tokens: {max_tokens}")
            logger.info(
                f"   - Tools enabled: {self.use_mcp_tools}, Tools count: {len(self.mcp_tools) if self.mcp_tools else 0}"
            )

            # Add context if provided
            if context:
                # If thinking is disabled, strip thinking blocks from context
                if not use_thinking:
                    context = self._strip_thinking_blocks(context)
                    logger.debug(
                        f"📋 Context: {len(context)} messages (thinking stripped)"
                    )
                else:
                    logger.debug(
                        f"📋 Context: {len(context)} messages (thinking preserved)"
                    )
                messages.extend(context)

            # Build user message content (support text, images, or mixed)
            user_content = self._build_user_content(message, images)
            messages.append({"role": "user", "content": user_content})

            # Add prefill if provided (assistant message to shape response)
            if prefill:
                messages.append({"role": "assistant", "content": prefill})

            # Prepare tools - combine both backend and MCP tools
            tools = []
            if self.use_backend_tools and self.backend_tools:
                tools.extend(self.backend_tools)
                logger.debug(
                    f"🔧 Backend tools enabled: {len(self.backend_tools)} tools available"
                )
            if self.use_mcp_tools and self.mcp_tools:
                tools.extend(self.mcp_tools)
                logger.debug(
                    f"🔧 MCP tools enabled: {len(self.mcp_tools)} tools available"
                )
            if not tools:
                tools = None

            # Use system prompt (default if not provided)
            effective_system_prompt = (
                system_prompt
                if system_prompt is not None
                else self.default_system_prompt
            )

            # Set thinking config
            thinking_config = None
            if use_thinking:
                budget = (
                    thinking_budget
                    if thinking_budget is not None
                    else self.thinking_budget
                )
                thinking_config = {"type": "enabled", "budget_tokens": budget}

            # Auto-summarize if context is too long (preserves context instead of dropping)
            messages, _summarized = self._prepare_context_sync(
                messages, effective_system_prompt, model=model
            )

            # Make API call
            # Note: Claude 4.5 requires using only temperature OR top_p, not both
            api_kwargs = {
                "model": model,
                "max_tokens": max_tokens,
                "messages": messages,
            }
            if effective_system_prompt:
                api_kwargs["system"] = effective_system_prompt
            if tools:
                api_kwargs["tools"] = tools
                logger.debug(f"🔧 MCP Tools enabled: {len(tools)} tools available")
            if thinking_config:
                api_kwargs["thinking"] = thinking_config
                logger.info(f"💭 Thinking config: {thinking_config}")

            logger.debug(f"🚀 Making API call with {len(messages)} messages")
            logger.debug(f"📋 API kwargs keys: {list(api_kwargs.keys())}")

            # OTEL: wrap the API call in a span and record GenAI metrics
            import time as _time

            _chat_span = None
            _chat_t0 = _time.monotonic()
            if _OTEL_CS_AVAILABLE:
                try:
                    _chat_span = _cs_tracer.start_span(
                        "claude.chat",
                        kind=SpanKind.CLIENT,
                        attributes={
                            "gen_ai.system": "anthropic",
                            "gen_ai.request.model": model,
                            "gen_ai.request.max_tokens": max_tokens,
                        },
                    )
                except Exception:
                    pass

            _call_started = _time.monotonic()
            response = self.client.messages.create(**api_kwargs)
            _call_duration_ms = int((_time.monotonic() - _call_started) * 1000)

            if _OTEL_CS_AVAILABLE and _chat_span is not None:
                try:
                    _duration = _time.monotonic() - _chat_t0
                    _in_tok = (
                        response.usage.input_tokens if hasattr(response, "usage") else 0
                    )
                    _out_tok = (
                        response.usage.output_tokens
                        if hasattr(response, "usage")
                        else 0
                    )
                    _model_used = (
                        response.model if hasattr(response, "model") else model
                    )
                    _chat_span.set_attribute("gen_ai.response.model", _model_used)
                    _chat_span.set_attribute("gen_ai.usage.input_tokens", _in_tok)
                    _chat_span.set_attribute("gen_ai.usage.output_tokens", _out_tok)
                    _chat_span.set_attribute(
                        "gen_ai.finish_reason", response.stop_reason or ""
                    )
                    _chat_span.end()
                    # Update metric counters
                    _labels = {
                        "gen_ai.system": "anthropic",
                        "gen_ai.request.model": _model_used,
                    }
                    _cs_genai_metrics["llm_calls"].add(1, _labels)
                    _cs_genai_metrics["llm_duration"].record(_duration, _labels)
                    _cs_genai_metrics["llm_tokens"].add(
                        _in_tok, {**_labels, "gen_ai.token.type": "input"}
                    )
                    _cs_genai_metrics["llm_tokens"].add(
                        _out_tok, {**_labels, "gen_ai.token.type": "output"}
                    )
                    from daemon.agent_runner import (
                        SONNET_INPUT_COST,
                        SONNET_OUTPUT_COST,
                    )

                    _cost = _in_tok * SONNET_INPUT_COST + _out_tok * SONNET_OUTPUT_COST
                    _cs_genai_metrics["llm_cost_usd"].add(_cost, _labels)
                except Exception:
                    pass

            # Persist reasoning trace (GH #79) — fire-and-forget, best-effort
            try:
                _usage = getattr(response, "usage", None)
                self._persist_interaction(
                    session_id=session_id,
                    agent_id=agent_id,
                    investigation_id=investigation_id,
                    model=getattr(response, "model", model),
                    system_prompt=effective_system_prompt,
                    request_messages=messages,
                    response_content=list(response.content) if response.content else [],
                    thinking_enabled=use_thinking,
                    thinking_budget=(
                        (
                            thinking_budget
                            if thinking_budget is not None
                            else self.thinking_budget
                        )
                        if use_thinking
                        else None
                    ),
                    stop_reason=getattr(response, "stop_reason", None),
                    input_tokens=getattr(_usage, "input_tokens", 0) if _usage else 0,
                    output_tokens=getattr(_usage, "output_tokens", 0) if _usage else 0,
                    cache_read_tokens=(
                        getattr(_usage, "cache_read_input_tokens", 0) if _usage else 0
                    ),
                    cache_creation_tokens=(
                        getattr(_usage, "cache_creation_input_tokens", 0)
                        if _usage
                        else 0
                    ),
                    duration_ms=_call_duration_ms,
                )
            except Exception as _pe:
                logger.debug(f"Reasoning-trace persist skipped: {_pe}")

            logger.debug(
                f"📥 API response received - stop_reason: {response.stop_reason}"
            )
            logger.debug(
                f"   - Response ID: {response.id if hasattr(response, 'id') else 'N/A'}"
            )
            logger.debug(
                f"   - Model: {response.model if hasattr(response, 'model') else 'N/A'}"
            )
            logger.debug(
                f"   - Content blocks: {len(response.content) if response.content else 0}"
            )

            # Handle stop reasons (including new refusal reason in Claude 4.5)
            if response.stop_reason == "refusal":
                logger.warning("❌ Claude refused to respond to the request")
                return "I apologize, but I cannot assist with that request."

            if response.stop_reason == "tool_use" and response.content:
                logger.info(f"🔧 Tool use detected - processing tools...")
                # Log tool calls
                for block in response.content:
                    if hasattr(block, "type") and block.type == "tool_use":
                        tool_name = getattr(block, "name", "unknown")
                        tool_input = getattr(block, "input", {})
                        logger.info(f"   🛠️  Tool call: {tool_name}")
                        logger.info(
                            f"      Input: {str(tool_input)[:200]}..."
                            if len(str(tool_input)) > 200
                            else f"      Input: {tool_input}"
                        )
                # Process tool use synchronously with timeout
                import asyncio

                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                try:
                    # Add overall timeout for tool processing (60 seconds total)
                    try:
                        # Route each tool call to the correct processor (backend or MCP)
                        tool_results = loop.run_until_complete(
                            asyncio.wait_for(
                                self._process_mixed_tool_use(response.content),
                                timeout=60.0,
                            )
                        )
                        logger.info(
                            f"✅ Tool processing complete - {len(tool_results)} results"
                        )
                        # Log tool results
                        for tool_result in tool_results:
                            result_content = tool_result.get("content", [])
                            logger.info(
                                f"   📊 Tool result: {str(result_content)[:200]}..."
                                if len(str(result_content)) > 200
                                else f"   📊 Tool result: {result_content}"
                            )
                    except asyncio.TimeoutError:
                        logger.error("⏱️ Tool processing timed out after 60 seconds")
                        # Return error message instead of hanging
                        return "I encountered a timeout while processing tool calls. The MCP servers may not be responding. Please check that the MCP servers are running (use the MCP Manager) and try again."

                    # Add tool results to messages and continue conversation
                    # Convert response.content to proper format if needed
                    assistant_content = response.content
                    if not isinstance(assistant_content, list):
                        assistant_content = (
                            [assistant_content] if assistant_content else []
                        )
                    messages.append({"role": "assistant", "content": assistant_content})
                    # Tool results need to be wrapped in a user message
                    messages.append({"role": "user", "content": tool_results})

                    # Get final response
                    api_kwargs = {
                        "model": model,
                        "max_tokens": max_tokens,
                        "messages": messages,
                    }
                    if effective_system_prompt:
                        api_kwargs["system"] = effective_system_prompt
                    if tools:
                        api_kwargs["tools"] = tools
                    # IMPORTANT: Include thinking config in follow-up request too!
                    if thinking_config:
                        api_kwargs["thinking"] = thinking_config

                    # Loop to handle multiple rounds of tool use (max 5 rounds)
                    for tool_round in range(5):
                        logger.debug(
                            f"🔁 Making follow-up API call after tool use (round {tool_round + 1})"
                        )
                        _fr_started = _time.monotonic()
                        final_response = self.client.messages.create(**api_kwargs)
                        _fr_duration_ms = int((_time.monotonic() - _fr_started) * 1000)
                        logger.debug(
                            f"📥 Final response received - stop_reason: {final_response.stop_reason}"
                        )

                        # Persist reasoning trace for this tool-loop iteration
                        try:
                            _fr_usage = getattr(final_response, "usage", None)
                            self._persist_interaction(
                                session_id=session_id,
                                agent_id=agent_id,
                                investigation_id=investigation_id,
                                model=getattr(final_response, "model", model),
                                system_prompt=effective_system_prompt,
                                request_messages=messages,
                                response_content=(
                                    list(final_response.content)
                                    if final_response.content
                                    else []
                                ),
                                thinking_enabled=use_thinking,
                                thinking_budget=(
                                    (
                                        thinking_budget
                                        if thinking_budget is not None
                                        else self.thinking_budget
                                    )
                                    if use_thinking
                                    else None
                                ),
                                stop_reason=getattr(
                                    final_response, "stop_reason", None
                                ),
                                input_tokens=(
                                    getattr(_fr_usage, "input_tokens", 0)
                                    if _fr_usage
                                    else 0
                                ),
                                output_tokens=(
                                    getattr(_fr_usage, "output_tokens", 0)
                                    if _fr_usage
                                    else 0
                                ),
                                cache_read_tokens=(
                                    getattr(_fr_usage, "cache_read_input_tokens", 0)
                                    if _fr_usage
                                    else 0
                                ),
                                cache_creation_tokens=(
                                    getattr(_fr_usage, "cache_creation_input_tokens", 0)
                                    if _fr_usage
                                    else 0
                                ),
                                duration_ms=_fr_duration_ms,
                            )
                        except Exception as _pe:
                            logger.debug(
                                f"Reasoning-trace persist skipped (tool round): {_pe}"
                            )

                        if final_response.stop_reason == "refusal":
                            logger.warning(
                                "❌ Claude refused to respond to the request"
                            )
                            return "I apologize, but I cannot assist with that request."

                        if (
                            final_response.stop_reason == "tool_use"
                            and final_response.content
                        ):
                            logger.info(
                                f"🔧 Additional tool use in round {tool_round + 2}"
                            )
                            additional_results = loop.run_until_complete(
                                asyncio.wait_for(
                                    self._process_mixed_tool_use(
                                        final_response.content
                                    ),
                                    timeout=60.0,
                                )
                            )
                            assistant_content = final_response.content
                            if not isinstance(assistant_content, list):
                                assistant_content = (
                                    [assistant_content] if assistant_content else []
                                )
                            messages.append(
                                {"role": "assistant", "content": assistant_content}
                            )
                            messages.append(
                                {"role": "user", "content": additional_results}
                            )
                            api_kwargs["messages"] = messages
                            continue

                        if final_response.content:
                            result = self._extract_content_blocks(
                                final_response.content, use_thinking
                            )
                            logger.info(
                                f"✅ Extracted content from final response - type: {type(result).__name__}"
                            )
                            return result

                        break

                    logger.warning("⚠️ Tool use loop exhausted without text response")
                    return None
                finally:
                    loop.close()

            # Extract all content blocks (including thinking blocks if enabled)
            if response.content:
                result = self._extract_content_blocks(response.content, use_thinking)
                logger.info(
                    f"✅ Extracted content from response - type: {type(result).__name__}"
                )
                if isinstance(result, list):
                    logger.info(f"   Content blocks: {len(result)} blocks")
                    for i, block in enumerate(result):
                        if isinstance(block, dict):
                            block_type = block.get("type", "unknown")
                            text_len = len(block.get("text", ""))
                            logger.info(
                                f"     Block {i}: {block_type} ({text_len} chars)"
                            )
                return result

            return None

        except Exception as e:
            logger.error(f"Error in Claude chat: {e}")
            raise

    def _build_user_content(
        self, message: Union[str, List[Dict]], images: Optional[List[Dict]] = None
    ) -> Union[str, List[Dict]]:
        """
        Build user content for API request, supporting text, images, or mixed content.

        Args:
            message: User message (string or list of content blocks).
            images: Optional list of image content blocks.

        Returns:
            Content for user message (string or list of content blocks).
        """
        # If message is already a list of content blocks, use it directly
        if isinstance(message, list):
            if images:
                # Merge images into existing content blocks
                return message + images
            return message

        # If images are provided, create mixed content
        if images:
            content_blocks = []
            # Add images first
            content_blocks.extend(images)
            # Add text message
            content_blocks.append({"type": "text", "text": message})
            return content_blocks

        # Simple text message
        return message

    def encode_image_base64(self, image_path: Union[str, Path]) -> str:
        """
        Encode an image file to base64.

        Args:
            image_path: Path to image file.

        Returns:
            Base64-encoded image string.
        """
        image_path = Path(image_path)
        if not image_path.exists():
            raise FileNotFoundError(f"Image file not found: {image_path}")

        with open(image_path, "rb") as image_file:
            return base64.b64encode(image_file.read()).decode("utf-8")

    def create_image_block(
        self,
        image_source: Union[str, Path, bytes],
        source_type: str = "auto",
        media_type: str = "image/jpeg",
    ) -> Dict:
        """
        Create an image content block for Claude API.

        Args:
            image_source: Image source (URL string, file path, or base64 bytes).
            source_type: "url", "base64", or "auto" (auto-detect from source).
            media_type: Media type (image/jpeg, image/png, image/gif, image/webp).

        Returns:
            Image content block dictionary.
        """
        if source_type == "auto":
            if isinstance(image_source, str):
                if image_source.startswith(("http://", "https://")):
                    source_type = "url"
                else:
                    source_type = "base64"
            elif isinstance(image_source, (Path, bytes)):
                source_type = "base64"

        if source_type == "url":
            return {
                "type": "image",
                "source": {"type": "url", "url": str(image_source)},
            }
        elif source_type == "base64":
            if isinstance(image_source, (str, Path)):
                data = self.encode_image_base64(image_source)
            elif isinstance(image_source, bytes):
                data = base64.b64encode(image_source).decode("utf-8")
            else:
                raise ValueError(f"Invalid image source type: {type(image_source)}")

            return {
                "type": "image",
                "source": {"type": "base64", "media_type": media_type, "data": data},
            }
        else:
            raise ValueError(
                f"Invalid source_type: {source_type}. Use 'url' or 'base64'."
            )

    async def chat_stream(
        self,
        message: Union[str, List[Dict]],
        system_prompt: Optional[str] = None,
        context: Optional[List[Dict]] = None,
        model: str = "claude-sonnet-4-5-20250929",
        images: Optional[List[Dict]] = None,
        prefill: Optional[str] = None,
        max_tokens: int = 4096,
        enable_thinking: Optional[bool] = None,
        thinking_budget: Optional[int] = None,
        session_id: Optional[str] = None,
        agent_id: Optional[str] = None,
        investigation_id: Optional[str] = None,
    ) -> AsyncIterator[str]:
        """
        Send a chat message to Claude with streaming response.

        Args:
            message: User message (string or list of content blocks for multimodal).
            system_prompt: Optional system prompt (uses default if None).
            context: Optional context messages.
            model: Claude model to use.
            images: Optional list of image content blocks (for vision).
            prefill: Optional prefill text to shape Claude's response.
            max_tokens: Maximum tokens for response (default: 4096).
            enable_thinking: Override thinking setting for this request.
            thinking_budget: Override thinking budget for this request.

        Yields:
            Text chunks as they arrive.
        """
        if not self.has_api_key():
            raise ValueError(
                "API key not configured. Please set your Anthropic API key."
            )

        try:
            messages = []

            # Determine thinking settings first
            use_thinking = (
                enable_thinking if enable_thinking is not None else self.enable_thinking
            )

            logger.info(
                f"🌊 ClaudeService.chat_stream() - Thinking: {use_thinking}, Budget: {thinking_budget or self.thinking_budget}, Model: {model}"
            )
            logger.info(f"📤 Streaming to Claude API:")
            logger.info(f"   - Message type: {type(message).__name__}")
            if isinstance(message, str):
                logger.info(
                    f"   - Message preview: {message[:200]}..."
                    if len(message) > 200
                    else f"   - Message: {message}"
                )
            else:
                logger.info(f"   - Message blocks: {len(message)}")
            logger.info(f"   - Context messages: {len(context) if context else 0}")

            if context:
                # If thinking is disabled, strip thinking blocks from context
                if not use_thinking:
                    context = self._strip_thinking_blocks(context)
                    logger.debug(
                        f"📋 Stream context: {len(context)} messages (thinking stripped)"
                    )
                else:
                    logger.debug(
                        f"📋 Stream context: {len(context)} messages (thinking preserved)"
                    )
                messages.extend(context)

            # Build user message content (support text, images, or mixed)
            user_content = self._build_user_content(message, images)
            messages.append({"role": "user", "content": user_content})

            # Add prefill if provided
            if prefill:
                messages.append({"role": "assistant", "content": prefill})

            # Prepare tools - combine both backend and MCP tools independently
            tools = []
            if self.use_backend_tools and self.backend_tools:
                tools.extend(self.backend_tools)
                logger.debug(f"🔧 Stream with {len(self.backend_tools)} backend tools")
            if self.use_mcp_tools and self.mcp_tools:
                tools.extend(self.mcp_tools)
                logger.debug(f"🔧 Stream with {len(self.mcp_tools)} MCP tools")
            if not tools:
                tools = None

            # Use system prompt (default if not provided)
            effective_system_prompt = (
                system_prompt
                if system_prompt is not None
                else self.default_system_prompt
            )

            # Set thinking config
            thinking_config = None
            if use_thinking:
                budget = (
                    thinking_budget
                    if thinking_budget is not None
                    else self.thinking_budget
                )
                thinking_config = {"type": "enabled", "budget_tokens": budget}

            # Auto-summarize if context is too long (preserves context instead of dropping)
            messages, summarized_count = await self._prepare_context_async(
                messages, effective_system_prompt, model=model
            )
            if summarized_count > 0:
                yield {
                    "type": "context_summarized",
                    "summarized_messages": summarized_count,
                    "remaining_messages": len(messages),
                }

            api_kwargs = {
                "model": model,
                "max_tokens": max_tokens,
                "messages": messages,
            }
            if effective_system_prompt:
                api_kwargs["system"] = effective_system_prompt
            if tools:
                api_kwargs["tools"] = tools
                logger.debug(f"🔧 Stream with {len(tools)} MCP tools")
            if thinking_config:
                api_kwargs["thinking"] = thinking_config
                logger.info(f"💭 Stream thinking config: {thinking_config}")

            # Stream with proper tool use handling using streaming API throughout
            max_iterations = 30  # Allow more iterations for complex workflows
            max_processing_time = 300  # 5 minutes maximum total processing time
            iteration = 0
            start_time = asyncio.get_event_loop().time()
            last_tool_calls = []  # Track recent tool calls to detect loops
            iteration_delays = []  # Track delays for rate limiting

            logger.debug(
                f"🚀 Starting stream iterations (max: {max_iterations}, max_time: {max_processing_time}s)"
            )

            while iteration < max_iterations:
                iteration += 1
                current_time = asyncio.get_event_loop().time()
                elapsed_time = current_time - start_time

                # Check if we've exceeded maximum processing time
                if elapsed_time > max_processing_time:
                    logger.warning(
                        f"⏱️ Maximum processing time ({max_processing_time}s) exceeded after {iteration} iterations"
                    )
                    yield {
                        "type": "text",
                        "content": "\n\n[Maximum processing time reached. Stopping to prevent timeout.]",
                    }
                    break

                logger.debug(
                    f"🔄 Stream iteration {iteration}/{max_iterations} (elapsed: {elapsed_time:.1f}s)"
                )

                # Add rate limiting delay between iterations (except first one)
                if iteration > 1:
                    # Start with 500ms delay, increase with exponential backoff if many iterations
                    base_delay = 0.5  # 500ms
                    if iteration > 15:
                        # Increase delay for later iterations to be more conservative
                        delay = base_delay * (1.5 ** (iteration - 15))
                    else:
                        delay = base_delay

                    # Cap delay at 3 seconds
                    delay = min(delay, 3.0)

                    logger.debug(
                        f"⏳ Rate limiting: waiting {delay:.2f}s before iteration {iteration}"
                    )
                    await asyncio.sleep(delay)
                    iteration_delays.append(delay)

                # Use streaming API to avoid timeout issues with tool use
                _stream_started = asyncio.get_event_loop().time()
                async with self.async_client.messages.stream(**api_kwargs) as stream:
                    accumulated_content = []
                    current_thinking_block = []
                    in_thinking = False

                    event_count = 0
                    thinking_event_count = 0
                    text_event_count = 0

                    # Handle different event types from the stream
                    async for event in stream:
                        event_count += 1
                        # Check event type
                        if hasattr(event, "type"):
                            event_type = event.type

                            # Handle content block start (including thinking blocks)
                            if event_type == "content_block_start":
                                if hasattr(event, "content_block"):
                                    block = event.content_block
                                    if (
                                        hasattr(block, "type")
                                        and block.type == "thinking"
                                    ):
                                        in_thinking = True
                                        current_thinking_block = []
                                        # Emit thinking block start marker
                                        logger.debug("💭 Thinking block started")
                                        yield {"type": "thinking_start"}

                            # Handle content block delta (text chunks)
                            elif event_type == "content_block_delta":
                                if hasattr(event, "delta"):
                                    delta = event.delta
                                    if hasattr(delta, "type"):
                                        if delta.type == "thinking_delta" and hasattr(
                                            delta, "thinking"
                                        ):
                                            # This is thinking content
                                            thinking_text = delta.thinking
                                            current_thinking_block.append(thinking_text)
                                            thinking_event_count += 1
                                            if thinking_event_count <= 2:
                                                logger.debug(
                                                    f"💭 Thinking delta: {thinking_text[:50]}..."
                                                )
                                            # Emit thinking chunk
                                            yield {
                                                "type": "thinking",
                                                "content": thinking_text,
                                            }
                                        elif delta.type == "text_delta" and hasattr(
                                            delta, "text"
                                        ):
                                            if not in_thinking:
                                                # Regular text content
                                                text_event_count += 1
                                                if text_event_count <= 2:
                                                    logger.debug(
                                                        f"📝 Text delta: {delta.text[:50]}..."
                                                    )
                                                yield {
                                                    "type": "text",
                                                    "content": delta.text,
                                                }

                            # Handle content block stop
                            elif event_type == "content_block_stop":
                                if in_thinking:
                                    in_thinking = False
                                    total_thinking = "".join(current_thinking_block)
                                    logger.info(
                                        f"💭 Thinking block ended - {len(total_thinking)} chars"
                                    )
                                    # Emit thinking block end marker
                                    yield {"type": "thinking_end"}

                    logger.debug(
                        f"📊 Stream events: total={event_count}, thinking={thinking_event_count}, text={text_event_count}"
                    )

                    # Get the final message to check for tool use
                    final_message = await stream.get_final_message()
                    accumulated_content = final_message.content
                    stop_reason = final_message.stop_reason

                    logger.debug(f"🏁 Stream stop reason: {stop_reason}")

                # Persist reasoning trace for this streaming iteration (GH #79)
                try:
                    _stream_duration_ms = int(
                        (asyncio.get_event_loop().time() - _stream_started) * 1000
                    )
                    _fm_usage = getattr(final_message, "usage", None)
                    await asyncio.to_thread(
                        self._persist_interaction,
                        session_id=session_id,
                        agent_id=agent_id,
                        investigation_id=investigation_id,
                        model=getattr(final_message, "model", model),
                        system_prompt=effective_system_prompt,
                        request_messages=messages,
                        response_content=(
                            list(accumulated_content) if accumulated_content else []
                        ),
                        thinking_enabled=use_thinking,
                        thinking_budget=(
                            (
                                thinking_budget
                                if thinking_budget is not None
                                else self.thinking_budget
                            )
                            if use_thinking
                            else None
                        ),
                        stop_reason=stop_reason,
                        input_tokens=(
                            getattr(_fm_usage, "input_tokens", 0) if _fm_usage else 0
                        ),
                        output_tokens=(
                            getattr(_fm_usage, "output_tokens", 0) if _fm_usage else 0
                        ),
                        cache_read_tokens=(
                            getattr(_fm_usage, "cache_read_input_tokens", 0)
                            if _fm_usage
                            else 0
                        ),
                        cache_creation_tokens=(
                            getattr(_fm_usage, "cache_creation_input_tokens", 0)
                            if _fm_usage
                            else 0
                        ),
                        duration_ms=_stream_duration_ms,
                    )
                except Exception as _pe:
                    logger.debug(f"Reasoning-trace persist skipped (stream): {_pe}")

                # Check if tool use is needed
                if stop_reason == "tool_use" and accumulated_content:
                    logger.info(f"🔧 Tool use in stream - processing...")

                    # Check for infinite loop detection
                    current_tool_calls = []
                    for block in accumulated_content:
                        if hasattr(block, "type") and block.type == "tool_use":
                            tool_name = getattr(block, "name", "unknown")
                            tool_input = getattr(block, "input", {})
                            tool_signature = f"{tool_name}:{str(tool_input)}"
                            current_tool_calls.append(tool_signature)

                    # Check if we're calling the same tools repeatedly (potential infinite loop)
                    if (
                        current_tool_calls
                        and current_tool_calls in last_tool_calls[-3:]
                    ):
                        logger.warning(
                            f"⚠️ Infinite loop detected - same tool calls repeated"
                        )
                        yield {
                            "type": "text",
                            "content": "\n\n[Detected repeated tool calls. Stopping to prevent infinite loop.]",
                        }
                        break

                    last_tool_calls.append(current_tool_calls)
                    # Keep only last 5 tool call sets for comparison
                    if len(last_tool_calls) > 5:
                        last_tool_calls.pop(0)

                    yield {"type": "text", "content": "\n\n[Processing tools...]\n"}

                    # Route each tool call to the correct processor (backend or MCP)
                    tool_results = await self._process_mixed_tool_use(
                        accumulated_content
                    )
                    logger.info(
                        f"✅ Tool processing complete in stream - {len(tool_results)} results"
                    )

                    # Add assistant message and tool results to conversation
                    messages.append(
                        {"role": "assistant", "content": accumulated_content}
                    )
                    messages.append({"role": "user", "content": tool_results})

                    # Update api_kwargs for next iteration with tool results
                    api_kwargs["messages"] = messages
                else:
                    # Done - no more tool use needed
                    total_elapsed = asyncio.get_event_loop().time() - start_time
                    total_delay = sum(iteration_delays)
                    logger.info(
                        f"✅ Stream complete after {iteration} iteration(s) in {total_elapsed:.1f}s (rate limiting: {total_delay:.1f}s)"
                    )
                    break

        except Exception as e:
            logger.error(f"Error in Claude chat stream: {e}")
            raise

    def analyze_finding(self, finding: Dict) -> str:
        """
        Analyze a security finding using Claude.

        Args:
            finding: Finding dictionary.

        Returns:
            Analysis text.
        """
        system_prompt = (
            "You are a security analyst helping to analyze security findings. "
            "Provide clear, actionable analysis of security findings including "
            "threat assessment, recommended actions, and context."
        )

        # Build a clean copy: drop embedding and strip None values for a cleaner prompt
        clean = {k: v for k, v in finding.items() if v is not None and k != "embedding"}
        finding_text = json.dumps(clean, indent=2, default=str)

        message = f"Analyze this security finding:\n\n{finding_text}\n\nProvide a detailed analysis."

        return self.chat(
            message, system_prompt=system_prompt, model="claude-sonnet-4-5-20250929"
        )

    def correlate_findings(self, findings: List[Dict]) -> str:
        """
        Correlate multiple findings using Claude.

        Args:
            findings: List of finding dictionaries.

        Returns:
            Correlation analysis text.
        """
        system_prompt = (
            "You are a security analyst correlating multiple security findings. "
            "Identify patterns, relationships, and potential attack campaigns. "
            "Provide insights on how findings relate to each other."
        )

        clean_findings = [
            {k: v for k, v in f.items() if v is not None and k != "embedding"}
            for f in findings
        ]
        findings_text = json.dumps(clean_findings, indent=2, default=str)

        message = f"Correlate these security findings:\n\n{findings_text}\n\nProvide correlation analysis."

        return self.chat(
            message, system_prompt=system_prompt, model="claude-sonnet-4-5-20250929"
        )

    def generate_case_summary(self, case: Dict, findings: List[Dict]) -> str:
        """
        Generate a case summary using Claude.

        Args:
            case: Case dictionary.
            findings: List of related findings.

        Returns:
            Case summary text.
        """
        system_prompt = (
            "You are a security analyst creating case summaries. "
            "Provide clear, concise summaries of investigation cases including "
            "key findings, threat assessment, and recommended next steps."
        )

        case_text = json.dumps(
            {k: v for k, v in case.items() if v is not None}, indent=2, default=str
        )
        clean_findings = [
            {k: v for k, v in f.items() if v is not None and k != "embedding"}
            for f in findings
        ]
        findings_text = json.dumps(clean_findings, indent=2, default=str)

        message = (
            f"Generate a summary for this investigation case:\n\n"
            f"Case:\n{case_text}\n\n"
            f"Related Findings:\n{findings_text}\n\n"
            f"Provide a comprehensive case summary."
        )

        return self.chat(
            message, system_prompt=system_prompt, model="claude-sonnet-4-5-20250929"
        )

    async def generate_event_analysis(
        self,
        event_data: Dict,
        related_events: List[Dict],
        finding_data: Optional[Dict] = None,
    ) -> Dict[str, Any]:
        """
        Generate comprehensive incident analysis for a timeline event.

        This method provides AI-powered analysis for SOC analysts to quickly understand
        security events in context.

        Args:
            event_data: The main event data
            related_events: List of related events in the time window
            finding_data: Optional associated finding data

        Returns:
            Dictionary with analysis fields:
            - incident_summary: Plain language summary of what happened
            - attack_narrative: Story of the attack based on event sequence
            - entity_analysis: Explanation of entity relationships
            - threat_assessment: Risk level and severity justification
            - investigation_priorities: What to investigate next
            - response_recommendations: Immediate recommended actions
            - timeline_correlation: How this event fits in the timeline
            - confidence_score: Confidence in the analysis (0.0-1.0)
        """
        system_prompt = """You are an expert SOC analyst providing incident analysis for timeline events.

Your analysis should help SOC analysts quickly understand:
- What happened in this security event
- How it relates to other events
- What entities (IPs, hosts, users) are involved
- What threat it represents
- What to investigate next
- What actions to take

Provide clear, actionable analysis in JSON format. Be concise but thorough.
Focus on practical insights that help with investigation and response."""

        # Prepare event context
        event_time = event_data.get("start", "")
        event_type = event_data.get("type", "unknown")
        event_severity = event_data.get("severity", "unknown")
        event_metadata = event_data.get("metadata", {})

        # Build context about entities (handles both singular and plural field formats)
        entities_summary = ""
        if finding_data and finding_data.get("entity_context"):
            entity_ctx = finding_data["entity_context"]
            entities_list = []
            src_ips = entity_ctx.get("src_ips") or []
            if not src_ips and entity_ctx.get("src_ip"):
                src_ips = [entity_ctx["src_ip"]]
            dst_ips = entity_ctx.get("dst_ips") or entity_ctx.get("dest_ips") or []
            if not dst_ips and entity_ctx.get("dst_ip"):
                dst_ips = [entity_ctx["dst_ip"]]
            hostnames = entity_ctx.get("hostnames") or []
            if not hostnames and entity_ctx.get("hostname"):
                hostnames = [entity_ctx["hostname"]]
            users = entity_ctx.get("users") or entity_ctx.get("usernames") or []
            if not users and entity_ctx.get("user"):
                users = [entity_ctx["user"]]
            if src_ips:
                entities_list.append(
                    f"Source IPs: {', '.join(str(ip) for ip in src_ips[:5])}"
                )
            if dst_ips:
                entities_list.append(
                    f"Destination IPs: {', '.join(str(ip) for ip in dst_ips[:5])}"
                )
            if hostnames:
                entities_list.append(
                    f"Hosts: {', '.join(str(h) for h in hostnames[:5])}"
                )
            if users:
                entities_list.append(f"Users: {', '.join(str(u) for u in users[:5])}")
            entities_summary = "\n".join(entities_list)

        # Build related events context
        related_summary = ""
        if related_events:
            related_summary = (
                f"\n{len(related_events)} related events in time window:\n"
            )
            for i, re in enumerate(related_events[:10], 1):
                re_time = re.get("start", "")
                re_sev = re.get("severity", "unknown")
                re_content = re.get("content", "")[:100]
                related_summary += f"{i}. [{re_sev}] {re_time} - {re_content}\n"

        # Build finding context
        finding_summary = ""
        if finding_data:
            desc = finding_data.get("description") or "N/A"
            finding_summary = f"""
Associated Finding:
- ID: {finding_data.get('finding_id') or 'N/A'}
- Severity: {finding_data.get('severity') or 'unknown'}
- Data Source: {finding_data.get('data_source') or 'unknown'}
- Anomaly Score: {float(finding_data.get('anomaly_score') or 0)}
- Description: {desc[:200]}
"""
            mitre_preds = finding_data.get("mitre_predictions") or {}
            if mitre_preds:
                top_techniques = sorted(
                    mitre_preds.items(), key=lambda x: float(x[1] or 0), reverse=True
                )[:3]
                finding_summary += f"\nTop MITRE Techniques: {', '.join([f'{t[0]} ({float(t[1] or 0):.2f})' for t in top_techniques])}"

        prompt = f"""Analyze this security event and provide comprehensive incident analysis.

EVENT DETAILS:
- Time: {event_time}
- Type: {event_type}
- Severity: {event_severity}
- Content: {event_data.get('content', '')}

{finding_summary}

ENTITIES INVOLVED:
{entities_summary if entities_summary else 'No entity information available'}

RELATED EVENTS:
{related_summary if related_summary else 'No related events in time window'}

Provide analysis in the following JSON format:
{{
  "incident_summary": "2-3 sentence plain language summary of what happened",
  "attack_narrative": "Story explaining the attack sequence and progression",
  "entity_analysis": "Explanation of how entities are connected and their roles",
  "threat_assessment": "Risk level assessment and severity justification",
  "investigation_priorities": ["Priority 1", "Priority 2", "Priority 3"],
  "response_recommendations": ["Action 1", "Action 2", "Action 3"],
  "timeline_correlation": "How this event fits in the bigger picture",
  "confidence_score": 0.85
}}

Provide only the JSON, no additional text."""

        try:
            # Use chat method to get analysis
            response = self.chat(
                prompt, system_prompt=system_prompt, model="claude-sonnet-4-5-20250929"
            )

            # Parse JSON response
            # Claude might wrap it in markdown code blocks, so handle that
            response_text = response.strip()
            if response_text.startswith("```json"):
                response_text = response_text[7:]
            if response_text.startswith("```"):
                response_text = response_text[3:]
            if response_text.endswith("```"):
                response_text = response_text[:-3]
            response_text = response_text.strip()

            analysis = json.loads(response_text)

            # Validate required fields
            required_fields = [
                "incident_summary",
                "attack_narrative",
                "entity_analysis",
                "threat_assessment",
                "investigation_priorities",
                "response_recommendations",
                "timeline_correlation",
            ]
            for field in required_fields:
                if field not in analysis:
                    analysis[field] = f"Analysis for {field} not available"

            if "confidence_score" not in analysis:
                analysis["confidence_score"] = 0.7

            return analysis

        except json.JSONDecodeError as e:
            logger.error(f"Failed to parse event analysis JSON: {e}")
            # Return fallback analysis
            return {
                "incident_summary": "AI analysis generated but could not be parsed properly.",
                "attack_narrative": "Event analysis is available but needs manual review.",
                "entity_analysis": "Entity relationships detected in event data.",
                "threat_assessment": f"Event severity: {event_severity}",
                "investigation_priorities": [
                    "Review event details",
                    "Check entity context",
                    "Correlate with related events",
                ],
                "response_recommendations": [
                    "Investigate further",
                    "Monitor related systems",
                    "Review security logs",
                ],
                "timeline_correlation": "Event occurred in the specified time window with related security events.",
                "confidence_score": 0.5,
                "error": "JSON parsing failed",
            }
        except Exception as e:
            logger.error(f"Error generating event analysis: {e}")
            raise

    # Agent SDK Methods

    async def agent_query(
        self,
        prompt: str,
        system_prompt: Optional[str] = None,
        allowed_tools: Optional[List[str]] = None,
        max_turns: int = 10,
        session_id: Optional[str] = None,
        model: str = "claude-sonnet-4-5-20250929",
    ) -> AsyncIterator[Dict[str, Any]]:
        """
        Run an agentic workflow using Claude Agent SDK with streaming.

        Args:
            prompt: The user prompt/task
            system_prompt: Optional system prompt
            allowed_tools: List of allowed tools (defaults to MCP tools + built-in)
            max_turns: Maximum conversation turns for the agent
            session_id: Optional session ID for conversation continuity
            model: Claude model to use

        Yields:
            Message events from the agent
        """
        if not AGENT_SDK_AVAILABLE:
            logger.warning("Agent SDK not available, falling back to standard chat")
            async for chunk in self.chat_stream(
                prompt, system_prompt=system_prompt, model=model
            ):
                yield {"type": "text", "content": chunk}
            return

        if not self.has_api_key():
            raise ValueError("API key not configured")

        # Build allowed tools list - combine MCP tools with Agent SDK built-ins
        tools = allowed_tools or []
        if not tools:
            # Default to useful built-in tools from Agent SDK
            tools = ["Read", "Grep", "Glob", "WebSearch"]
            # Add MCP tool names
            if self.mcp_tools:
                tools.extend([t["name"] for t in self.mcp_tools])

        effective_system = system_prompt or self.default_system_prompt

        # Build MCP server configurations for Agent SDK
        mcp_servers = self._get_agent_sdk_mcp_servers()

        # Configure agent options
        agent_options_kwargs = {
            "system_prompt": effective_system,
            "allowed_tools": tools,
            "max_turns": max_turns,
            "model": model,
        }
        if mcp_servers:
            agent_options_kwargs["mcp_servers"] = mcp_servers

        options = ClaudeAgentOptions(**agent_options_kwargs)

        # Track session context — use get_session() to trigger L2 MemPalace restore
        context = (self.get_session(session_id) or []) if session_id else []

        try:
            async for message in agent_query(prompt=prompt, options=options):
                # Process different message types
                if hasattr(message, "type"):
                    msg_type = message.type

                    if msg_type == "text":
                        content = getattr(message, "content", "") or getattr(
                            message, "text", ""
                        )
                        yield {"type": "text", "content": content}

                    elif msg_type == "tool_use":
                        tool_name = getattr(message, "name", "unknown")
                        tool_input = getattr(message, "input", {})
                        yield {
                            "type": "tool_use",
                            "tool": tool_name,
                            "input": tool_input,
                        }
                        # If this is an MCP tool, execute it
                        if "_" in tool_name and self.use_mcp_tools:
                            result = await self._execute_mcp_tool(tool_name, tool_input)
                            yield {
                                "type": "tool_result",
                                "tool": tool_name,
                                "result": result,
                            }

                    elif msg_type == "tool_result":
                        yield {
                            "type": "tool_result",
                            "tool": getattr(message, "name", "unknown"),
                            "result": getattr(message, "content", ""),
                        }

                    elif msg_type == "result" or msg_type == "end":
                        result = getattr(message, "result", "") or getattr(
                            message, "content", ""
                        )
                        yield {"type": "result", "content": result}

                elif hasattr(message, "result"):
                    yield {"type": "result", "content": message.result}

                elif hasattr(message, "content"):
                    yield {"type": "text", "content": message.content}

            # Update session if tracking
            if session_id:
                self.sessions[session_id] = context + [
                    {"role": "user", "content": prompt}
                ]
                self._persist_session_async(session_id)

        except Exception as e:
            logger.error(f"Agent query error: {e}")
            yield {"type": "error", "content": str(e)}

    def _get_agent_sdk_mcp_servers(self) -> List[Dict]:
        """
        Build MCP server configurations for the Agent SDK.

        Only includes *enabled* servers. Reads from MCP registry (if available)
        or falls back to the security-detections server with dynamic env vars.
        """
        mcp_servers = []

        try:
            # Try the MCP registry first (Phase 3) - filter to enabled only
            from services.mcp_registry import get_mcp_registry
            from services.mcp_service import MCPService

            registry = get_mcp_registry()
            all_configs = registry.get_agent_sdk_configs()

            # Filter to only enabled servers
            try:
                from backend.api.mcp import mcp_service as _mcp_svc

                mcp_servers = [
                    c for c in all_configs if _mcp_svc.is_server_enabled(c["name"])
                ]
            except Exception:
                mcp_servers = all_configs  # fallback if service not importable

            if mcp_servers:
                logger.info(
                    f"Agent SDK: loaded {len(mcp_servers)} enabled MCP servers from registry"
                )
                return mcp_servers
        except (ImportError, Exception) as e:
            logger.debug(f"MCP registry not available, using fallback: {e}")

        # Fallback: configure security-detections MCP server directly
        try:
            from services.detection_rules_service import get_detection_rules_service

            detection_service = get_detection_rules_service()
            env_vars = detection_service.get_mcp_env_vars()

            if env_vars:
                mcp_servers.append(
                    {
                        "name": "security-detections",
                        "command": "npx",
                        "args": ["-y", "security-detections-mcp"],
                        "env": env_vars,
                    }
                )
                logger.info(
                    f"Agent SDK: configured security-detections MCP with {len(env_vars)} env vars"
                )
        except Exception as e:
            logger.warning(
                f"Could not configure security-detections for Agent SDK: {e}"
            )

        return mcp_servers

    async def _execute_mcp_tool(self, tool_name: str, arguments: Dict) -> str:
        """Execute an MCP tool via the tool name, with response size guard."""
        parts = tool_name.split("_", 1)
        if len(parts) != 2:
            return f"Invalid MCP tool format: {tool_name}"

        server_name, actual_tool_name = parts

        try:
            from services.mcp_client import get_mcp_client

            mcp_client = get_mcp_client()
            if mcp_client:
                result = await mcp_client.call_tool(
                    server_name, actual_tool_name, arguments, timeout=30.0
                )
                if isinstance(result, dict):
                    raw = json.dumps(result.get("content", result))
                else:
                    raw = str(result)
                return self._truncate_tool_response(raw)
        except Exception as e:
            logger.error(f"MCP tool execution error: {e}")
            return f"Error: {str(e)}"

        return "MCP client unavailable"

    async def run_agent_task(
        self,
        task: str,
        agent_config: Optional[Dict] = None,
        session_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Run a complete agent task and return the final result.

        Args:
            task: The task description
            agent_config: Optional agent configuration (system_prompt, tools, etc)
            session_id: Optional session ID for continuity

        Returns:
            Dict with result and any tool outputs
        """
        config = agent_config or {}
        system_prompt = config.get("system_prompt")
        allowed_tools = config.get("allowed_tools")
        max_turns = config.get("max_turns", 10)
        model = config.get("model", "claude-sonnet-4-5-20250929")

        results = {"task": task, "tool_calls": [], "final_result": "", "success": True}

        try:
            async for event in self.agent_query(
                prompt=task,
                system_prompt=system_prompt,
                allowed_tools=allowed_tools,
                max_turns=max_turns,
                session_id=session_id,
                model=model,
            ):
                event_type = event.get("type", "")

                if event_type == "tool_use":
                    results["tool_calls"].append(
                        {"tool": event.get("tool"), "input": event.get("input")}
                    )
                elif event_type == "tool_result":
                    if results["tool_calls"]:
                        results["tool_calls"][-1]["result"] = event.get("result")
                elif event_type == "result":
                    results["final_result"] = event.get("content", "")
                elif event_type == "text":
                    results["final_result"] += event.get("content", "")
                elif event_type == "error":
                    results["success"] = False
                    results["error"] = event.get("content", "")

        except Exception as e:
            results["success"] = False
            results["error"] = str(e)
            logger.error(f"Agent task error: {e}")

        return results

    def _get_mempalace_sessions_dir(self) -> Optional[Path]:
        """Return path to the sessions persistence directory inside the MemPalace data dir.

        We write session JSON files directly to the palace data directory because the
        MemPalace Python package exposes a Searcher/KnowledgeGraph API for reads, but
        does not expose a general-purpose write API — that is handled by the MCP server
        tools used by agents. Session persistence is a service-level concern, so we
        manage it ourselves with simple JSON files.
        """
        if self._mempalace is None:
            try:
                import os

                data_dir = Path(
                    os.environ.get(
                        "MEMPALACE_PALACE_PATH",
                        str(Path.home() / ".mempalace" / "palace"),
                    )
                )
                sessions_dir = data_dir / "sessions"
                sessions_dir.mkdir(parents=True, exist_ok=True)
                self._mempalace = sessions_dir
            except Exception as e:
                logger.debug(f"MemPalace sessions dir init failed: {e}")
                self._mempalace = False
        return self._mempalace if self._mempalace else None

    def _persist_session_async(self, session_id: str) -> None:
        """Fire-and-forget persistence of session to MemPalace data directory."""
        messages = list(self.sessions.get(session_id, []))

        def _write():
            sessions_dir = self._get_mempalace_sessions_dir()
            if not sessions_dir:
                return
            try:
                import os

                safe_id = session_id.replace("/", "_").replace("\\", "_")
                tmp = sessions_dir / f"{safe_id}.json.tmp"
                dest = sessions_dir / f"{safe_id}.json"
                tmp.write_text(
                    json.dumps({"messages": messages, "message_count": len(messages)})
                )
                os.replace(tmp, dest)
            except Exception as e:
                logger.debug(f"MemPalace session persist failed: {e}")

        threading.Thread(target=_write, daemon=True).start()

    def create_session(
        self, session_id: str, initial_context: Optional[List[Dict]] = None
    ) -> str:
        """Create or reset a conversation session."""
        self.sessions[session_id] = initial_context or []
        self._persist_session_async(session_id)
        return session_id

    def get_session(self, session_id: str) -> Optional[List[Dict]]:
        """Get session history, restoring from MemPalace on L1 cache miss."""
        if session_id in self.sessions:
            return self.sessions[session_id]

        # L2: attempt to restore from MemPalace sessions directory
        sessions_dir = self._get_mempalace_sessions_dir()
        if sessions_dir:
            try:
                safe_id = session_id.replace("/", "_").replace("\\", "_")
                session_file = sessions_dir / f"{safe_id}.json"
                if session_file.exists():
                    data = json.loads(session_file.read_text())
                    messages = data.get("messages", [])
                    if messages:
                        self.sessions[session_id] = messages
                        logger.info(
                            f"Restored session {session_id} from MemPalace "
                            f"({len(messages)} messages)"
                        )
                        return self.sessions[session_id]
            except Exception as e:
                logger.debug(f"MemPalace session restore failed: {e}")

        return None

    def clear_session(self, session_id: str) -> bool:
        """Clear a session."""
        if session_id in self.sessions:
            del self.sessions[session_id]
            return True
        return False

    @staticmethod
    def is_agent_sdk_available() -> bool:
        """Check if Agent SDK is available."""
        return AGENT_SDK_AVAILABLE
