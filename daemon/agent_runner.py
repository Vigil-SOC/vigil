"""Sub-agent runner for autonomous investigations.

Manages the lifecycle of individual investigation agents. Each sub-agent
runs in an async loop, reading its plan.md, executing steps via Claude
with full MCP/backend tool access, and writing results back to its
working directory.
"""

import asyncio
import json
import logging
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from daemon.config import OrchestratorConfig
from daemon.plan_generator import WORKFLOW_STEP_MAP, DEFAULT_STEPS
from daemon.workdir import WorkdirManager

logger = logging.getLogger(__name__)

try:
    from core.telemetry import get_tracer, extract_traceparent
    from opentelemetry.trace import SpanKind
    _tracer = get_tracer("vigil.daemon.agent_runner")
except Exception:
    _tracer = None  # type: ignore[assignment]

# TODO(#89): Sonnet price constants assume Anthropic. With multi-provider
# support (#88) landing, cost tracking should resolve per-provider rates
# from a model registry instead of a single global constant. Update
# together with the per-agent model assignment work in #89.
SONNET_INPUT_COST = 3.0 / 1_000_000
SONNET_OUTPUT_COST = 15.0 / 1_000_000

TOOL_TIERS = {
    "safe": [
        "list_findings", "get_finding", "search_findings", "nearest_neighbors",
        "get_findings_stats", "semantic_search_findings", "technique_rollup",
        "list_cases", "get_case", "get_case_comments", "get_case_iocs",
        "get_case_tasks", "search_detections", "get_coverage_stats",
        "get_detection_count", "analyze_coverage", "identify_gaps",
        "create_attack_layer", "get_attack_layer",
    ],
    "managed": [
        "create_case", "update_case", "add_finding_to_case",
        "bulk_add_findings_to_case", "remove_finding_from_case",
        "add_case_activity", "add_case_timeline_entry",
        "add_case_mitre_techniques", "add_resolution_step",
        "add_case_comment", "add_case_evidence", "add_case_ioc",
        "bulk_add_iocs", "add_case_task", "update_case_task",
        "link_related_cases", "escalate_case",
        "create_approval_action",
    ],
    "requires_approval": [
        "isolate_host", "block_ip", "disable_user",
        "quarantine_file", "close_case",
    ],
    "forbidden": [
        "delete_case", "delete_finding", "approve_action",
        "reject_action",
    ],
}

_TOOL_TIER_LOOKUP: Dict[str, str] = {}
for _tier, _tools in TOOL_TIERS.items():
    for _t in _tools:
        _TOOL_TIER_LOOKUP[_t] = _tier


def _get_tool_tier(tool_name: str) -> str:
    """Get the safety tier for a tool. Strips MCP server prefixes."""
    if tool_name in _TOOL_TIER_LOOKUP:
        return _TOOL_TIER_LOOKUP[tool_name]
    short = tool_name.split("_", 1)[-1] if "_" in tool_name else tool_name
    for tier, tools in TOOL_TIERS.items():
        if short in tools:
            return tier
    return "unknown"


WORKDIR_TOOLS = [
    {
        "name": "read_investigation_file",
        "description": "Read a file from the current investigation working directory.",
        "input_schema": {
            "type": "object",
            "properties": {
                "filename": {"type": "string", "description": "Relative path to file (e.g. 'iocs.json', 'evidence/query_results/scan1.json')"}
            },
            "required": ["filename"]
        }
    },
    {
        "name": "write_investigation_file",
        "description": "Write or overwrite a file in the current investigation working directory.",
        "input_schema": {
            "type": "object",
            "properties": {
                "filename": {"type": "string", "description": "Relative path to file"},
                "content": {"type": "string", "description": "File content to write"}
            },
            "required": ["filename", "content"]
        }
    },
    {
        "name": "append_investigation_file",
        "description": "Append content to a file in the current investigation working directory.",
        "input_schema": {
            "type": "object",
            "properties": {
                "filename": {"type": "string", "description": "Relative path to file"},
                "content": {"type": "string", "description": "Content to append"}
            },
            "required": ["filename", "content"]
        }
    },
    {
        "name": "list_investigation_files",
        "description": "List all files in the current investigation working directory.",
        "input_schema": {
            "type": "object",
            "properties": {}
        }
    },
    {
        "name": "update_plan_step",
        "description": "Update a step in plan.md. Changes status from [pending] to [in_progress], [completed], or [blocked]. Can also add result notes.",
        "input_schema": {
            "type": "object",
            "properties": {
                "step_number": {"type": "integer", "description": "Step number to update"},
                "status": {"type": "string", "enum": ["in_progress", "completed", "blocked"], "description": "New status"},
                "result_notes": {"type": "string", "description": "Optional notes about what was found/done"}
            },
            "required": ["step_number", "status"]
        }
    },
    {
        "name": "signal_complete",
        "description": "Signal that the investigation is complete and ready for master agent review. Call this when all plan steps are done.",
        "input_schema": {
            "type": "object",
            "properties": {
                "summary": {"type": "string", "description": "Brief summary of investigation findings"},
                "proposed_actions": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "action": {"type": "string"},
                            "target": {"type": "string"},
                            "reason": {"type": "string"},
                            "requires_approval": {"type": "boolean"}
                        }
                    },
                    "description": "List of proposed response actions"
                }
            },
            "required": ["summary"]
        }
    },
]


class AgentRunner:
    """Manages the pool of running sub-agent tasks."""

    def __init__(self, config: OrchestratorConfig, workdir_mgr: WorkdirManager):
        self.config = config
        self.workdir = workdir_mgr
        self._active_agents: Dict[str, asyncio.Task] = {}
        self._semaphore = asyncio.Semaphore(config.max_concurrent_agents)
        self._claude_service = None
        self._data_service = None
        self._llm_gateway = None

        self.stats = {
            "agents_started": 0,
            "agents_completed": 0,
            "agents_failed": 0,
            "total_iterations": 0,
            "total_cost_usd": 0.0,
        }

    def _init_services(self):
        if self._claude_service is None:
            try:
                from services.claude_service import ClaudeService
                self._claude_service = ClaudeService(
                    use_backend_tools=True,
                    use_mcp_tools=True,
                    use_agent_sdk=False,
                    enable_thinking=True,
                    thinking_budget=8000,
                )
                logger.info("AgentRunner: Claude service initialized")
            except Exception as e:
                logger.error(f"AgentRunner: Failed to init Claude service: {e}")

        if self._data_service is None:
            try:
                from services.database_data_service import DatabaseDataService
                self._data_service = DatabaseDataService()
            except Exception as e:
                logger.error(f"AgentRunner: Failed to init data service: {e}")

    async def _ensure_gateway(self):
        """Lazily initialise the LLM gateway."""
        if self._llm_gateway is None:
            try:
                from services.llm_gateway import get_llm_gateway
                self._llm_gateway = await get_llm_gateway()
                logger.info("AgentRunner: LLM gateway connected")
            except Exception as e:
                logger.error(f"AgentRunner: Failed to connect LLM gateway: {e}")

    @property
    def active_count(self) -> int:
        return sum(1 for t in self._active_agents.values() if not t.done())

    def is_running(self, investigation_id: str) -> bool:
        task = self._active_agents.get(investigation_id)
        return task is not None and not task.done()

    async def start_agent(self, investigation: Dict[str, Any], shutdown_event: asyncio.Event):
        """Start a sub-agent for an investigation."""
        inv_id = investigation["investigation_id"]
        if self.is_running(inv_id):
            logger.warning(f"Agent already running for {inv_id}")
            return

        self._init_services()
        await self._ensure_gateway()
        task = asyncio.create_task(self._run_agent(investigation, shutdown_event))
        self._active_agents[inv_id] = task
        self.stats["agents_started"] += 1
        logger.info(f"Started sub-agent for {inv_id}")

    async def stop_agent(self, investigation_id: str):
        """Cancel a running agent task."""
        task = self._active_agents.get(investigation_id)
        if task and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            logger.info(f"Stopped agent for {investigation_id}")

    async def stop_all(self):
        for inv_id in list(self._active_agents.keys()):
            await self.stop_agent(inv_id)

    @staticmethod
    def _get_step_title(workflow_id: str, step_num: int) -> str:
        steps = WORKFLOW_STEP_MAP.get(workflow_id, DEFAULT_STEPS)
        idx = max(0, step_num - 1)
        if idx < len(steps):
            return steps[idx]["title"]
        return f"Step {step_num}"

    async def _run_agent(self, investigation: Dict[str, Any], shutdown_event: asyncio.Event):
        """The main sub-agent loop for a single investigation."""
        inv_id = investigation["investigation_id"]
        start_time = time.time()
        iteration = 0
        total_input_tokens = 0
        total_output_tokens = 0
        total_cost = 0.0

        # Re-attach to the root investigation span as a child span
        _agent_span = None
        try:
            if _tracer is not None:
                _tp = investigation.get("otel_traceparent", "")
                _parent_ctx = extract_traceparent({"traceparent": _tp}) if _tp else None
                _agent_span = _tracer.start_span(
                    "agent_run",
                    context=_parent_ctx,
                    kind=SpanKind.INTERNAL,
                    attributes={
                        "vigil.investigation.id": inv_id,
                        "vigil.investigation.workflow_id": investigation.get("workflow_id", ""),
                    },
                )
        except Exception:
            pass

        try:
            while not shutdown_event.is_set():
                state = self.workdir.read_state(inv_id)
                if state.get("status") in ("completed", "failed", "sleeping", "review_submitted"):
                    break

                if state.get("status") == "waiting_approval":
                    resolved = await self._check_approval(inv_id, state, start_time)
                    if resolved is None:
                        break
                    if not resolved:
                        await asyncio.sleep(30)
                        continue
                    state = self.workdir.read_state(inv_id)

                iteration += 1
                if iteration > self.config.max_iterations_per_agent:
                    logger.warning(f"{inv_id}: Max iterations ({self.config.max_iterations_per_agent}) exceeded")
                    self._mark_failed(inv_id, "Max iterations exceeded")
                    break

                if total_cost >= self.config.max_cost_per_investigation:
                    logger.warning(f"{inv_id}: Cost budget (${self.config.max_cost_per_investigation}) exceeded")
                    self._mark_failed(inv_id, "Cost budget exceeded")
                    break

                elapsed = time.time() - start_time
                if elapsed > self.config.max_runtime_per_investigation:
                    logger.warning(f"{inv_id}: Runtime limit ({self.config.max_runtime_per_investigation}s) exceeded")
                    self._mark_failed(inv_id, "Runtime limit exceeded")
                    break

                self.workdir.append_log(inv_id, {
                    "event": "iteration_start",
                    "iteration": iteration,
                    "elapsed_seconds": round(elapsed, 1),
                    "cost_usd": round(total_cost, 4),
                })

                workflow_id = investigation.get("workflow_id") or state.get("workflow_id", "")
                step_title = self._get_step_title(workflow_id, state.get("current_step", 1))
                self._update_db_record(inv_id, {"current_activity": step_title})

                plan = self.workdir.read_file(inv_id, "plan.md")
                prompt = self._build_prompt(inv_id, plan, state, iteration)

                # Iteration-level span
                _iter_span = None
                try:
                    if _tracer is not None:
                        _iter_span = _tracer.start_span(
                            "iteration",
                            kind=SpanKind.INTERNAL,
                            attributes={
                                "vigil.investigation.id": inv_id,
                                "vigil.agent.iteration": iteration,
                                "vigil.investigation.current_step": state.get("current_step", 1),
                            },
                        )
                except Exception:
                    pass

                try:
                    result = await self._call_claude(inv_id, prompt)
                except Exception as e:
                    logger.error(f"{inv_id}: Claude call failed: {e}")
                    self.workdir.append_log(inv_id, {"event": "error", "error": str(e)})
                    if _iter_span is not None:
                        try:
                            _iter_span.end()
                        except Exception:
                            pass
                    state["error_count"] = state.get("error_count", 0) + 1
                    if state["error_count"] >= 3:
                        self._mark_failed(inv_id, f"Repeated errors: {e}")
                        break
                    self.workdir.write_state(inv_id, state)
                    await asyncio.sleep(min(30, 5 * state["error_count"]))
                    continue

                in_tokens = result.get("input_tokens", 0)
                out_tokens = result.get("output_tokens", 0)
                cost = in_tokens * SONNET_INPUT_COST + out_tokens * SONNET_OUTPUT_COST
                total_input_tokens += in_tokens
                total_output_tokens += out_tokens
                total_cost += cost
                self.stats["total_iterations"] += 1
                self.stats["total_cost_usd"] += cost

                self.workdir.append_log(inv_id, {
                    "event": "iteration_complete",
                    "iteration": iteration,
                    "input_tokens": in_tokens,
                    "output_tokens": out_tokens,
                    "cost_usd": round(cost, 4),
                    "tool_calls": len(result.get("tool_calls", [])),
                })

                refreshed = self.workdir.read_state(inv_id)

                self._update_db_record(inv_id, {
                    "iteration_count": iteration,
                    "input_tokens": total_input_tokens,
                    "output_tokens": total_output_tokens,
                    "cost_usd": round(total_cost, 4),
                    "last_activity_at": datetime.utcnow().isoformat(),
                    "current_step": refreshed.get("current_step", 0),
                })

                # Close iteration span with token/cost summary
                try:
                    if _iter_span is not None:
                        _iter_span.set_attribute("gen_ai.usage.input_tokens", in_tokens)
                        _iter_span.set_attribute("gen_ai.usage.output_tokens", out_tokens)
                        _iter_span.end()
                except Exception:
                    pass

                if refreshed.get("status") in ("completed", "review_submitted", "failed"):
                    break

                await asyncio.sleep(self.config.agent_loop_delay)

        except asyncio.CancelledError:
            logger.info(f"{inv_id}: Agent cancelled")
            state = self.workdir.read_state(inv_id)
            state["status"] = "sleeping"
            self.workdir.write_state(inv_id, state)
        except Exception as e:
            logger.error(f"{inv_id}: Unexpected agent error: {e}", exc_info=True)
            self._mark_failed(inv_id, str(e))
            self.stats["agents_failed"] += 1
        else:
            final_state = self.workdir.read_state(inv_id)
            if final_state.get("status") == "review_submitted":
                self.stats["agents_completed"] += 1
            elif final_state.get("status") == "failed":
                self.stats["agents_failed"] += 1
            self._update_db_record(inv_id, {
                "current_step": final_state.get("current_step", 0),
            })
        finally:
            self._active_agents.pop(inv_id, None)
            try:
                if _agent_span is not None:
                    _agent_span.set_attribute("vigil.agent.total_iterations", iteration)
                    _agent_span.set_attribute("gen_ai.usage.input_tokens", total_input_tokens)
                    _agent_span.set_attribute("gen_ai.usage.output_tokens", total_output_tokens)
                    _agent_span.end()
            except Exception:
                pass

    def _build_prompt(self, inv_id: str, plan: str, state: Dict, iteration: int) -> str:
        """Build the Claude prompt from plan, context, and state."""
        context_md = self.workdir.read_file(inv_id, "context.md")
        if len(context_md) > self.config.context_max_chars:
            keep_head = 2000
            keep_tail = self.config.context_max_chars - keep_head - 100
            context_md = context_md[:keep_head] + "\n\n...(earlier context summarized)...\n\n" + context_md[-keep_tail:]

        current_step = state.get("current_step", 1)
        total_steps = state.get("total_steps", 0)
        workflow_id = state.get("workflow_id", "unknown")
        budget_remaining = self.config.max_cost_per_investigation - state.get("cost_usd", 0.0)

        case_id = state.get("case_id")
        is_case_review = workflow_id == "case-review"

        if is_case_review:
            preamble = f"""You are an autonomous SOC case-review agent reviewing case {case_id}.
You are on iteration {iteration} of your review loop.

CRITICAL RULES:
- Use tools to gather real data. Never speculate when you can query.
- After completing each step, call update_plan_step to mark it done and add results.
- When ALL steps are complete, call signal_complete with a summary.
- You have a budget of ${budget_remaining:.2f} remaining. Be efficient with tool calls.
- Focus on Step {current_step} of {total_steps}. Complete it, then advance."""

            instructions = f"""## Instructions
You are reviewing case {case_id}. Your job is to generate resolution steps, root cause
analysis, and recommendations based on all the evidence gathered by investigation agents.

1. Start by calling get_case with case_id "{case_id}" to load the full case data.
2. For each finding_id in the case, call get_finding to read the finding details.
3. Analyze the aggregated evidence to determine root cause and attack chain.
4. Use add_resolution_step to record each concrete resolution action:
   - Containment steps (e.g., "Isolate host X from network")
   - Eradication steps (e.g., "Remove malware from host X")
   - Recovery steps (e.g., "Restore services, monitor for re-infection")
   - Validation steps (e.g., "Verify no further C2 beaconing")
5. Use update_case to write an executive summary into the case description.
6. Write your analysis to context.md using append_investigation_file.
7. When done, call signal_complete with a summary of resolution steps created.

After each tool call, analyze the results and decide your next action.
Do NOT repeat tool calls you've already made unless checking for updates."""
        else:
            preamble = f"""You are an autonomous SOC investigation agent running the "{workflow_id}" workflow.
You are on iteration {iteration} of your investigation loop.

CRITICAL RULES:
- Use tools to gather real data. Never speculate when you can query.
- After completing each step, call update_plan_step to mark it done and add results.
- When ALL steps are complete, call signal_complete with a summary and proposed actions.
- Write important findings to context.md using append_investigation_file.
- Store discovered IOCs in iocs.json, timeline events in timeline.json.
- You have a budget of ${budget_remaining:.2f} remaining. Be efficient with tool calls.
- If you encounter a blocker, update the plan step as [blocked] and move to the next step.
- Focus on Step {current_step} of {total_steps}. Complete it, then advance."""

            instructions = """## Instructions
Execute the current pending step in the plan. Use the available tools:
1. MCP tools (list_findings, get_finding, nearest_neighbors, search_detections, etc.) to query data
2. Case management tools (create_case, update_case, add_finding_to_case, etc.) to manage the investigation case
3. Working directory tools (read/write/append_investigation_file) to persist your findings
4. update_plan_step to track progress
5. signal_complete when the investigation is finished

## CASE MANAGEMENT RULES (CRITICAL)
You MUST place findings into the correct case. Follow this process:
1. BEFORE creating a new case, ALWAYS call list_cases to check for existing open cases.
2. Look for cases that share overlapping entities (same IPs, hostnames, users, domains) or
   related MITRE techniques. If a relevant open case exists, use add_finding_to_case to add
   your finding(s) to it instead of creating a duplicate case.
3. Only call create_case when NO existing case covers the same incident, entities, or campaign.
   Include all investigated finding IDs and set an appropriate severity/priority.
4. After adding findings to a case (existing or new), update the case with your analysis:
   - Use add_case_activity to log what you discovered
   - Use add_case_timeline_entry for key events
   - Use add_case_mitre_techniques for mapped TTPs
   - Use add_case_ioc with discovered IOCs (IPs, domains, hashes)
5. If you find that two or more existing cases are actually part of the same incident,
   use link_related_cases to connect them (relationship_type: "related" or "duplicate").

After each tool call, analyze the results and decide your next action.
Do NOT repeat tool calls you've already made unless checking for updates."""

        sections = [
            preamble,
            f"## Current Investigation Plan\n\n{plan}",
            f"## Investigation Context\n\n{context_md}" if context_md.strip() else "",
            f"## Current State\n```json\n{json.dumps(state, indent=2, default=str)}\n```",
            instructions,
        ]

        return "\n\n".join(s for s in sections if s)

    async def _call_claude(self, inv_id: str, prompt: str) -> Dict[str, Any]:
        """Execute a Claude call with tools, handling tool use in a loop.

        Each individual API call is routed through the LLM gateway / ARQ
        queue, which enforces global rate limiting and runs the actual
        Anthropic call inside the worker process.  Tool execution still
        happens locally in this process.
        """
        if self._llm_gateway is None:
            raise RuntimeError("LLM gateway not connected")

        all_tools = list(WORKDIR_TOOLS)

        try:
            if self._claude_service and hasattr(self._claude_service, 'backend_tools') and self._claude_service.backend_tools:
                all_tools.extend(self._claude_service.backend_tools)
        except Exception:
            pass

        try:
            from services.mcp_registry import get_mcp_registry
            registry = get_mcp_registry()
            mcp_schemas = registry.get_all_tools()
            if mcp_schemas:
                backend_names = {t["name"] for t in all_tools}
                for tool in mcp_schemas:
                    raw_name = tool["name"].split("_", 1)[-1] if "_" in tool["name"] else tool["name"]
                    if raw_name not in backend_names:
                        all_tools.append(tool)
        except Exception:
            pass

        messages = [{"role": "user", "content": prompt}]
        tool_calls_made = []
        total_input = 0
        total_output = 0
        max_turns = 25

        thinking_enabled = getattr(self._claude_service, 'enable_thinking', False) if self._claude_service else True
        thinking_budget = getattr(self._claude_service, 'thinking_budget', 8000) if self._claude_service else 8000
        max_tok = max(16000, thinking_budget + 4096) if thinking_enabled else 4096

        for turn in range(max_turns):
            # LLM call span (one per tool-use round)
            _llm_span = None
            try:
                if _tracer is not None:
                    _llm_span = _tracer.start_span(
                        "llm_call",
                        kind=SpanKind.CLIENT,
                        attributes={
                            "vigil.investigation.id": inv_id,
                            "gen_ai.system": "anthropic",
                            "gen_ai.request.model": self.config.plan_model,
                            "gen_ai.tool_use.round": turn,
                        },
                    )
            except Exception:
                pass

            try:
                response = await self._llm_gateway.submit_investigation_turn(
                    inv_id=inv_id,
                    messages=messages,
                    model=self.config.plan_model,
                    max_tokens=max_tok,
                    enable_thinking=thinking_enabled,
                    thinking_budget=thinking_budget,
                    tools=all_tools if all_tools else None,
                    timeout=180,
                )
            except Exception as e:
                logger.error(f"{inv_id}: LLM queue error: {e}")
                try:
                    if _llm_span is not None:
                        _llm_span.end()
                except Exception:
                    pass
                raise

            try:
                if _llm_span is not None:
                    _llm_span.set_attribute("gen_ai.usage.input_tokens", response.get("input_tokens", 0))
                    _llm_span.set_attribute("gen_ai.usage.output_tokens", response.get("output_tokens", 0))
                    _llm_span.set_attribute("gen_ai.finish_reason", response.get("stop_reason", ""))
                    _llm_span.end()
            except Exception:
                pass

            total_input += response.get("input_tokens", 0)
            total_output += response.get("output_tokens", 0)

            stop_reason = response.get("stop_reason", "end_turn")
            if stop_reason == "end_turn" or stop_reason != "tool_use":
                break

            content_blocks = response.get("content", [])
            tool_use_blocks = [b for b in content_blocks if b.get("type") == "tool_use"]
            if not tool_use_blocks:
                break

            messages.append({"role": "assistant", "content": content_blocks})

            tool_results = []
            for tool_block in tool_use_blocks:
                tool_name = tool_block["name"]
                tool_input = tool_block["input"]
                tool_calls_made.append({"tool": tool_name, "input": tool_input})

                self._update_db_record(inv_id, {"current_activity": f"Calling {tool_name}"})
                result = await self._execute_tool(inv_id, tool_name, tool_input)
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": tool_block["id"],
                    "content": str(result)[:10000],
                })

            messages.append({"role": "user", "content": tool_results})

        return {
            "input_tokens": total_input,
            "output_tokens": total_output,
            "tool_calls": tool_calls_made,
        }

    async def _execute_tool(self, inv_id: str, tool_name: str, tool_input: Dict) -> str:
        """Execute a tool call, routing between workdir tools, backend tools, and MCP tools."""
        try:
            if tool_name == "read_investigation_file":
                return self.workdir.read_file(inv_id, tool_input["filename"])

            elif tool_name == "write_investigation_file":
                self.workdir.write_file(inv_id, tool_input["filename"], tool_input["content"])
                return f"Written to {tool_input['filename']}"

            elif tool_name == "append_investigation_file":
                self.workdir.append_file(inv_id, tool_input["filename"], tool_input["content"])
                return f"Appended to {tool_input['filename']}"

            elif tool_name == "list_investigation_files":
                files = self.workdir.list_files(inv_id)
                return json.dumps(files)

            elif tool_name == "update_plan_step":
                return self._handle_update_plan_step(inv_id, tool_input)

            elif tool_name == "signal_complete":
                return self._handle_signal_complete(inv_id, tool_input)

            else:
                return await self._execute_external_tool(inv_id, tool_name, tool_input)

        except Exception as e:
            logger.error(f"{inv_id}: Tool {tool_name} error: {e}")
            return f"Error: {e}"

    def _handle_update_plan_step(self, inv_id: str, tool_input: Dict) -> str:
        step_num = tool_input["step_number"]
        status = tool_input["status"]
        notes = tool_input.get("result_notes", "")

        plan = self.workdir.read_file(inv_id, "plan.md")

        pattern = rf"(### Step {step_num}:.*?)\[(pending|in_progress|blocked)\]"
        replacement = rf"\1[{status}]"
        updated = re.sub(pattern, replacement, plan)

        if notes and status == "completed":
            step_pattern = rf"(### Step {step_num}:.*?\[completed\]\n(?:- .*\n)*)"
            match = re.search(step_pattern, updated)
            if match:
                insert_point = match.end()
                note_block = f"  - **Result:** {notes}\n"
                updated = updated[:insert_point] + note_block + updated[insert_point:]

        self.workdir.write_file(inv_id, "plan.md", updated)

        state = self.workdir.read_state(inv_id)
        if status == "completed":
            completed = state.get("completed_steps", [])
            if step_num not in completed:
                completed.append(step_num)
            state["completed_steps"] = completed
            if step_num >= state.get("current_step", 1):
                state["current_step"] = step_num + 1
        elif status == "in_progress":
            state["current_step"] = step_num
        state["last_update"] = datetime.utcnow().isoformat()
        self.workdir.write_state(inv_id, state)

        return f"Step {step_num} updated to [{status}]"

    def _handle_signal_complete(self, inv_id: str, tool_input: Dict) -> str:
        summary = tool_input["summary"]
        proposed = tool_input.get("proposed_actions", [])

        state = self.workdir.read_state(inv_id)
        state["status"] = "review_submitted"
        state["current_step"] = state.get("total_steps", state.get("current_step", 0))
        state["summary"] = summary
        state["proposed_actions"] = proposed
        state["completed_at"] = datetime.utcnow().isoformat()
        self.workdir.write_state(inv_id, state)

        review = [
            "# Investigation Review",
            "",
            f"**Investigation:** {inv_id}",
            f"**Completed:** {datetime.utcnow().isoformat()}",
            "",
            "## Summary",
            summary,
            "",
        ]
        if proposed:
            review.append("## Proposed Actions")
            review.append("")
            for action in proposed:
                approval = " [REQUIRES APPROVAL]" if action.get("requires_approval") else ""
                review.append(f"- **{action.get('action', 'N/A')}** on {action.get('target', 'N/A')}: {action.get('reason', '')}{approval}")
            review.append("")

        self.workdir.write_file(inv_id, "review.md", "\n".join(review))

        self._update_db_record(inv_id, {
            "status": "review_submitted",
            "summary": summary,
            "proposed_actions": proposed,
            "completed_at": datetime.utcnow().isoformat(),
            "current_step": state.get("total_steps", state.get("current_step", 0)),
            "current_activity": "Complete",
        })

        return "Investigation marked as complete. Awaiting master agent review."

    async def _execute_external_tool(self, inv_id: str, tool_name: str, tool_input: Dict) -> str:
        """Route to backend or MCP tool execution, enforcing safety guardrails."""
        import time as _time
        tier = _get_tool_tier(tool_name)

        _tool_span = None
        _t0 = _time.monotonic()
        try:
            if _tracer is not None:
                _tool_span = _tracer.start_span(
                    "tool_call",
                    kind=SpanKind.CLIENT,
                    attributes={
                        "vigil.investigation.id": inv_id,
                        "vigil.tool.name": tool_name,
                        "vigil.tool.tier": tier,
                        "vigil.tool.input_size": len(json.dumps(tool_input, default=str)),
                    },
                )
        except Exception:
            pass

        if tier == "forbidden":
            msg = f"Tool '{tool_name}' is forbidden for autonomous agents."
            logger.warning(f"{inv_id}: Blocked forbidden tool call: {tool_name}")
            self.workdir.append_log(inv_id, {
                "event": "tool_blocked", "tool": tool_name, "tier": "forbidden",
            })
            try:
                if _tool_span is not None:
                    _tool_span.set_attribute("vigil.tool.success", False)
                    _tool_span.end()
            except Exception:
                pass
            return msg

        if self.config.dry_run and tier in ("managed", "requires_approval"):
            msg = f"[DRY RUN] Would execute {tool_name} with {json.dumps(tool_input, default=str)}"
            self.workdir.append_log(inv_id, {
                "event": "dry_run_skip", "tool": tool_name, "tier": tier,
            })
            return msg

        if tier == "requires_approval":
            return await self._request_tool_approval(inv_id, tool_name, tool_input)

        if self._claude_service and hasattr(self._claude_service, '_execute_backend_tool'):
            try:
                result = await self._claude_service._execute_backend_tool(
                    tool_name, tool_input
                )
                if result is not None:
                    _r = json.dumps(result, default=str) if not isinstance(result, str) else result
                    try:
                        if _tool_span is not None:
                            _tool_span.set_attribute("vigil.tool.success", True)
                            _tool_span.set_attribute("vigil.tool.output_size", len(_r))
                            _tool_span.set_attribute("vigil.tool.duration_ms", round((_time.monotonic() - _t0) * 1000, 1))
                            _tool_span.end()
                    except Exception:
                        pass
                    return _r
            except Exception:
                pass

        try:
            from services.mcp_client import get_mcp_client
            client = get_mcp_client()
            if client:
                server_name = None
                actual_tool_name = tool_name

                if "_" in tool_name:
                    prefix, suffix = tool_name.split("_", 1)
                    if prefix in (client.tools_cache or {}):
                        server_name = prefix
                        actual_tool_name = suffix

                if server_name is None:
                    for srv_name, tools in (client.tools_cache or {}).items():
                        if any(t["name"] == tool_name for t in tools):
                            server_name = srv_name
                            actual_tool_name = tool_name
                            break

                if server_name:
                    result = await client.call_tool(server_name, actual_tool_name, tool_input)
                    if result is not None:
                        _r = json.dumps(result, default=str) if not isinstance(result, str) else result
                        try:
                            if _tool_span is not None:
                                _tool_span.set_attribute("vigil.tool.success", True)
                                _tool_span.set_attribute("vigil.tool.output_size", len(_r))
                                _tool_span.set_attribute("vigil.tool.duration_ms", round((_time.monotonic() - _t0) * 1000, 1))
                                _tool_span.end()
                        except Exception:
                            pass
                        return _r
        except Exception as e:
            logger.debug(f"MCP tool {tool_name} call failed: {e}")

        _result_str = f"Tool '{tool_name}' not found or unavailable"
        try:
            if _tool_span is not None:
                _tool_span.set_attribute("vigil.tool.success", False)
                _tool_span.set_attribute("vigil.tool.duration_ms", round((_time.monotonic() - _t0) * 1000, 1))
                _tool_span.end()
        except Exception:
            pass
        return _result_str

    async def _request_tool_approval(self, inv_id: str, tool_name: str, tool_input: Dict) -> str:
        """Create an approval request and put the agent into waiting_approval state."""
        try:
            from services.approval_service import get_approval_service, ActionType
            service = get_approval_service()

            try:
                action_type = ActionType(tool_name)
            except ValueError:
                action_type = ActionType.CUSTOM

            pending = service.create_action(
                action_type=action_type,
                title=f"Auto-investigation tool: {tool_name}",
                description=f"Investigation {inv_id} requests execution of {tool_name}",
                target=tool_input.get("target", tool_input.get("ip", tool_input.get("host", "unknown"))),
                confidence=0.7,
                reason=f"Autonomous investigation {inv_id} needs to execute {tool_name}",
                evidence=[inv_id],
                created_by="orchestrator",
                parameters=tool_input,
            )
            action_id = pending.action_id

            state = self.workdir.read_state(inv_id)
            state["status"] = "waiting_approval"
            state["pending_approval"] = {
                "action_id": action_id,
                "tool_name": tool_name,
                "tool_input": tool_input,
                "requested_at": datetime.utcnow().isoformat(),
            }
            self.workdir.write_state(inv_id, state)

            self._update_db_record(inv_id, {"status": "waiting_approval"})

            self.workdir.append_log(inv_id, {
                "event": "approval_requested",
                "tool": tool_name,
                "action_id": action_id,
            })

            logger.info(f"{inv_id}: Approval requested for {tool_name} (action_id={action_id})")
            return f"Tool '{tool_name}' requires approval. Approval request created (action_id={action_id}). The agent will pause until the request is resolved."

        except Exception as e:
            logger.error(f"{inv_id}: Failed to create approval for {tool_name}: {e}")
            return f"Error creating approval for '{tool_name}': {e}"

    async def _check_approval(self, inv_id: str, state: Dict, start_time: float) -> Optional[bool]:
        """Check if a pending approval has been resolved.

        Returns True if approved and agent should continue, False if still
        pending (caller should sleep and retry), None if the agent should stop
        (timeout or fatal).
        """
        pending = state.get("pending_approval", {})
        action_id = pending.get("action_id")
        if not action_id:
            state["status"] = "executing"
            self.workdir.write_state(inv_id, state)
            self._update_db_record(inv_id, {"status": "executing"})
            return True

        elapsed = time.time() - start_time
        if elapsed > self.config.max_runtime_per_investigation:
            self._mark_failed(inv_id, "Runtime limit exceeded while waiting for approval")
            return None

        try:
            from services.approval_service import get_approval_service, ActionStatus
            service = get_approval_service()
            action = service.get_action(action_id)

            if action is None:
                state["status"] = "executing"
                state.pop("pending_approval", None)
                self.workdir.write_state(inv_id, state)
                self._update_db_record(inv_id, {"status": "executing"})
                return True

            if action.status == ActionStatus.APPROVED:
                logger.info(f"{inv_id}: Approval {action_id} APPROVED, resuming")
                state["status"] = "executing"
                state.pop("pending_approval", None)
                self.workdir.write_state(inv_id, state)
                self._update_db_record(inv_id, {"status": "executing"})
                self.workdir.append_log(inv_id, {
                    "event": "approval_granted", "action_id": action_id,
                    "tool": pending.get("tool_name"),
                })

                tool_name = pending.get("tool_name")
                tool_input = pending.get("tool_input", {})
                if tool_name:
                    result = await self._execute_approved_tool(tool_name, tool_input)
                    self.workdir.append_file(inv_id, "context.md",
                        f"\n\n### Approved tool result: {tool_name}\n```\n{result[:2000]}\n```\n")
                return True

            if action.status == ActionStatus.REJECTED:
                reason = getattr(action, "rejection_reason", "No reason provided")
                logger.info(f"{inv_id}: Approval {action_id} REJECTED: {reason}")
                state["status"] = "executing"
                state.pop("pending_approval", None)
                self.workdir.write_state(inv_id, state)
                self._update_db_record(inv_id, {"status": "executing"})

                self.workdir.append_file(inv_id, "context.md",
                    f"\n\n### Approval REJECTED for {pending.get('tool_name', 'unknown')}\nReason: {reason}\nAgent must find an alternative approach.\n")

                plan = self.workdir.read_file(inv_id, "plan.md")
                step = state.get("current_step", 1)
                plan += f"\n\n> **Note (Step {step}):** Tool `{pending.get('tool_name')}` was rejected. Reason: {reason}\n"
                self.workdir.write_file(inv_id, "plan.md", plan)

                self.workdir.append_log(inv_id, {
                    "event": "approval_rejected", "action_id": action_id,
                    "tool": pending.get("tool_name"), "reason": reason,
                })
                return True

            return False

        except Exception as e:
            logger.error(f"{inv_id}: Error checking approval {action_id}: {e}")
            return False

    async def _execute_approved_tool(self, tool_name: str, tool_input: Dict) -> str:
        """Execute a tool that has already been approved, bypassing guardrails."""
        if self._claude_service and hasattr(self._claude_service, '_execute_backend_tool'):
            try:
                result = await self._claude_service._execute_backend_tool(
                    tool_name, tool_input
                )
                if result is not None:
                    return json.dumps(result, default=str) if not isinstance(result, str) else result
            except Exception:
                pass
        try:
            from services.mcp_client import get_mcp_client
            client = get_mcp_client()
            if client:
                result = await client.call_tool(tool_name, tool_input)
                if result is not None:
                    return json.dumps(result, default=str) if not isinstance(result, str) else result
        except Exception as e:
            logger.debug(f"Approved tool {tool_name} failed: {e}")
        return f"Tool '{tool_name}' execution failed"

    def _mark_failed(self, inv_id: str, reason: str):
        state = self.workdir.read_state(inv_id)
        state["status"] = "failed"
        state["failure_reason"] = reason
        state["failed_at"] = datetime.utcnow().isoformat()
        self.workdir.write_state(inv_id, state)
        self.workdir.append_log(inv_id, {"event": "failed", "reason": reason})
        self._update_db_record(inv_id, {
            "status": "failed",
            "last_error": reason,
            "current_activity": "Failed",
        })

    def _update_db_record(self, inv_id: str, updates: Dict[str, Any]):
        """Update the Investigation record in the database."""
        if not self._data_service:
            return
        try:
            from database.connection import get_session
            from database.models import Investigation
            session = get_session()
            inv = session.query(Investigation).filter_by(investigation_id=inv_id).first()
            if inv:
                for key, val in updates.items():
                    if hasattr(inv, key):
                        if key.endswith("_at") and isinstance(val, str):
                            val = datetime.fromisoformat(val)
                        setattr(inv, key, val)
                session.commit()
        except Exception as e:
            logger.debug(f"DB update for {inv_id} failed: {e}")
