"""Workflows service for discovering, parsing, and executing WORKFLOW.md workflow definitions."""

import asyncio
import logging
import re
from typing import Dict, List, Optional, Any
from pathlib import Path
from datetime import datetime

logger = logging.getLogger(__name__)


def _parse_yaml_frontmatter(content: str) -> Dict[str, Any]:
    """
    Parse YAML frontmatter from a WORKFLOW.md file.

    Uses simple regex parsing to avoid pyyaml dependency.
    Handles strings, lists (both inline [...] and indented - item).
    """
    # Match frontmatter block: --- ... --- followed by newline, EOF, or content
    match = re.match(r"^---\s*\n(.*?)\n---\s*(?:\n|$)", content, re.DOTALL)
    if not match:
        return {}

    frontmatter_text = match.group(1)
    result = {}
    current_key = None
    current_list = None

    for line in frontmatter_text.split("\n"):
        # Skip empty lines and comments
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue

        # Check for list continuation (indented "- item")
        if current_key and current_list is not None and re.match(r"^\s+-\s+", line):
            item = re.sub(r"^\s+-\s+", "", line).strip().strip('"').strip("'")
            current_list.append(item)
            result[current_key] = current_list
            continue

        # Key-value pair
        kv_match = re.match(r"^(\S+):\s*(.*)", line)
        if kv_match:
            key = kv_match.group(1)
            value = kv_match.group(2).strip()
            current_key = key
            current_list = None

            if not value:
                # Might be start of a list
                current_list = []
                result[key] = current_list
            elif value.startswith("[") and value.endswith("]"):
                # Inline list: [item1, item2, ...]
                items = value[1:-1].split(",")
                result[key] = [
                    i.strip().strip('"').strip("'") for i in items if i.strip()
                ]
                current_list = None
            elif value.startswith('"') and value.endswith('"'):
                result[key] = value[1:-1]
                current_list = None
            elif value.startswith("'") and value.endswith("'"):
                result[key] = value[1:-1]
                current_list = None
            else:
                result[key] = value
                current_list = None

    return result


def _get_frontmatter_end(content: str) -> int:
    """Get the character index where frontmatter ends and body begins."""
    match = re.match(r"^---\s*\n(.*?)\n---\s*(?:\n|$)", content, re.DOTALL)
    if match:
        return match.end()
    return 0


class WorkflowDefinition:
    """Represents a parsed workflow from a WORKFLOW.md file."""

    def __init__(
        self,
        workflow_id: str,
        file_path: Optional[Path],
        metadata: Dict[str, Any],
        body: str,
        source: str = "file",
    ):
        self.id = workflow_id
        self.file_path = file_path
        self.metadata = metadata
        self.body = body
        self.source = source  # "file" or "custom"

    @property
    def name(self) -> str:
        return self.metadata.get("name", self.id)

    @property
    def description(self) -> str:
        return self.metadata.get("description", "")

    @property
    def agents(self) -> List[str]:
        agents = self.metadata.get("agents", [])
        if isinstance(agents, str):
            return [a.strip() for a in agents.split(",")]
        return agents

    @property
    def tools_used(self) -> List[str]:
        tools = self.metadata.get("tools-used", [])
        if isinstance(tools, str):
            return [t.strip() for t in tools.split(",")]
        return tools

    @property
    def use_case(self) -> str:
        return self.metadata.get("use-case", "")

    @property
    def trigger_examples(self) -> List[str]:
        examples = self.metadata.get("trigger-examples", [])
        if isinstance(examples, str):
            return [examples]
        return examples

    def to_dict(self, include_body: bool = False) -> Dict[str, Any]:
        """Convert to JSON-serializable dictionary."""
        result = {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "agents": self.agents,
            "tools_used": self.tools_used,
            "use_case": self.use_case,
            "trigger_examples": self.trigger_examples,
            "source": self.source,
        }
        if include_body:
            result["body"] = self.body
        # Custom workflows carry structured phases for the builder UI
        if "phases" in self.metadata:
            result["phases"] = self.metadata["phases"]
        return result


def _custom_workflow_to_definition(wf: Dict[str, Any]) -> WorkflowDefinition:
    """
    Adapt a database-backed custom workflow dict into a WorkflowDefinition so
    that existing execution code (build_execution_prompt, execute_workflow)
    can consume it without changes.
    """
    phases = wf.get("phases") or []
    agents: List[str] = []
    tools: List[str] = []
    for phase in phases:
        agent_id = phase.get("agent_id")
        if agent_id and agent_id not in agents:
            agents.append(agent_id)
        for tool in phase.get("tools", []) or []:
            if tool not in tools:
                tools.append(tool)

    metadata = {
        "name": wf.get("name", wf.get("workflow_id")),
        "description": wf.get("description", ""),
        "agents": agents,
        "tools-used": tools,
        "use-case": wf.get("use_case", ""),
        "trigger-examples": wf.get("trigger_examples") or [],
        "phases": phases,
    }

    body = _render_custom_workflow_body(wf, phases)
    return WorkflowDefinition(
        workflow_id=wf["workflow_id"],
        file_path=None,
        metadata=metadata,
        body=body,
        source="custom",
    )


def _render_custom_workflow_body(
    wf: Dict[str, Any], phases: List[Dict[str, Any]]
) -> str:
    """Render a markdown body from structured phases, compatible with
    build_execution_prompt()'s template."""
    lines: List[str] = []
    lines.append(f"# {wf.get('name', wf.get('workflow_id'))}")
    if wf.get("description"):
        lines.append("")
        lines.append(wf["description"])
    lines.append("")
    lines.append("## Agent Sequence")
    lines.append("")
    for phase in phases:
        order = phase.get("order", "?")
        name = phase.get("name", f"Phase {order}")
        agent = phase.get("agent_id", "")
        lines.append(f"### Phase {order}: {name} ({agent})")
        if phase.get("purpose"):
            lines.append("")
            lines.append(f"**Purpose:** {phase['purpose']}")
        tools = phase.get("tools") or []
        if tools:
            lines.append("")
            lines.append("**Tools:** " + ", ".join(f"`{t}`" for t in tools))
        steps = phase.get("steps") or []
        if steps:
            lines.append("")
            lines.append("**Steps:**")
            for i, step in enumerate(steps, start=1):
                lines.append(f"{i}. {step}")
        if phase.get("expected_output"):
            lines.append("")
            lines.append(f"**Output:** {phase['expected_output']}")
        if phase.get("approval_required"):
            lines.append("")
            lines.append("**Approval required before executing this phase.**")
        lines.append("")
    return "\n".join(lines).strip()


class WorkflowsService:
    """Service for discovering, parsing, and executing workflow definitions."""

    def __init__(self, workflows_dir: Optional[Path] = None):
        """
        Initialize workflows service.

        Args:
            workflows_dir: Directory containing workflow definitions (default: ./workflows)
        """
        if workflows_dir is None:
            workflows_dir = Path(__file__).parent.parent / "workflows"

        self.workflows_dir = Path(workflows_dir)
        self._cache: Dict[str, WorkflowDefinition] = {}
        self._cache_loaded_at: Optional[datetime] = None

        # Load workflows on init
        self._load_workflows()

    def _load_workflows(self):
        """Discover and parse all WORKFLOW.md files from the workflows directory."""
        self._cache.clear()

        if not self.workflows_dir.exists():
            logger.warning(f"Workflows directory not found: {self.workflows_dir}")
            return

        for workflow_dir in sorted(self.workflows_dir.iterdir()):
            if not workflow_dir.is_dir():
                continue

            workflow_file = workflow_dir / "WORKFLOW.md"
            if not workflow_file.exists():
                continue

            try:
                content = workflow_file.read_text(encoding="utf-8")
                metadata = _parse_yaml_frontmatter(content)
                body_start = _get_frontmatter_end(content)
                body = content[body_start:].strip()

                workflow_id = workflow_dir.name
                workflow = WorkflowDefinition(
                    workflow_id=workflow_id,
                    file_path=workflow_file,
                    metadata=metadata,
                    body=body,
                )

                self._cache[workflow_id] = workflow
                logger.info(f"Loaded workflow: {workflow_id} ({workflow.name})")

            except Exception as e:
                logger.error(f"Error loading workflow from {workflow_file}: {e}")

        self._cache_loaded_at = datetime.now()
        logger.info(f"Loaded {len(self._cache)} workflows from {self.workflows_dir}")

    def reload(self):
        """Force reload all workflows from disk."""
        self._load_workflows()

    def _get_custom_workflow(self, workflow_id: str) -> Optional[WorkflowDefinition]:
        """Fetch a single custom workflow from the database by ID."""
        try:
            from services.custom_workflow_service import get_custom_workflow_service

            raw = get_custom_workflow_service().get(workflow_id)
        except Exception as e:
            logger.debug(f"Custom workflow lookup failed for {workflow_id}: {e}")
            return None
        if not raw or not raw.get("is_active", True):
            return None
        return _custom_workflow_to_definition(raw)

    def _list_custom_workflows(self) -> List[WorkflowDefinition]:
        """List active custom workflows from the database."""
        try:
            from services.custom_workflow_service import get_custom_workflow_service

            rows = get_custom_workflow_service().list(active_only=True)
        except Exception as e:
            logger.debug(f"Custom workflow listing failed: {e}")
            return []
        return [_custom_workflow_to_definition(r) for r in rows]

    def list_workflows(self) -> List[Dict[str, Any]]:
        """
        Return metadata for all discovered workflows, merging file-based and
        database-backed custom workflows. Custom workflows are listed first.
        """
        custom = [
            wf.to_dict(include_body=False) for wf in self._list_custom_workflows()
        ]
        file_based = [wf.to_dict(include_body=False) for wf in self._cache.values()]
        return custom + file_based

    def get_workflow(self, workflow_id: str) -> Optional[WorkflowDefinition]:
        """Get a specific workflow by ID (custom workflows take precedence)."""
        custom = self._get_custom_workflow(workflow_id)
        if custom:
            return custom
        return self._cache.get(workflow_id)

    def get_workflow_dict(
        self, workflow_id: str, include_body: bool = True
    ) -> Optional[Dict[str, Any]]:
        """Get a specific workflow as a dictionary."""
        workflow = self.get_workflow(workflow_id)
        if workflow:
            return workflow.to_dict(include_body=include_body)
        return None

    def build_execution_prompt(
        self,
        workflow: WorkflowDefinition,
        target_context: str,
        agent_profiles: Optional[Dict] = None,
    ) -> str:
        """
        Build a composite prompt that instructs Claude to execute a workflow.

        Embeds the workflow's full instructions plus relevant agent methodologies
        into a single prompt for ClaudeService.run_agent_task().

        Args:
            workflow: The workflow definition to execute
            target_context: Context about the target (finding details, case details, etc.)
            agent_profiles: Optional dict of agent_id -> AgentProfile for embedding methodologies

        Returns:
            Composite prompt string
        """
        # Build agent methodology section
        agent_section = ""
        if agent_profiles:
            agent_section = "\n\n## Agent Methodologies\n\n"
            agent_section += "You will be executing this workflow by embodying each agent in sequence. "
            agent_section += (
                "Here are the methodologies for each agent you will use:\n\n"
            )

            for agent_id in workflow.agents:
                profile = agent_profiles.get(agent_id)
                if profile:
                    agent_section += f"### {profile.name} ({agent_id})\n"
                    agent_section += f"**Specialization:** {profile.specialization}\n"
                    agent_section += f"**Description:** {profile.description}\n"
                    # Extract methodology from system prompt
                    methodology_match = re.search(
                        r"<methodology>(.*?)</methodology>",
                        profile.system_prompt,
                        re.DOTALL,
                    )
                    if methodology_match:
                        agent_section += (
                            f"**Methodology:**\n{methodology_match.group(1).strip()}\n"
                        )
                    agent_section += "\n"

        prompt = f"""# Execute Workflow: {workflow.name}

## Workflow Description
{workflow.description}

## Target Context
{target_context}

## Workflow Instructions

You are executing the **{workflow.name}** workflow. Follow each phase in order,
using the specified tools to gather data and build context between phases.
Pass the outputs of each phase as input context to the next phase.

For each phase:
1. Announce which phase you are starting and which agent role you are adopting
2. Follow the agent's methodology for that phase
3. Use the specified tools to gather data
4. Summarize your findings before moving to the next phase
5. When all phases are complete, provide a final consolidated summary

{workflow.body}
{agent_section}

## Execution Rules

- Execute ALL phases in order. Do not skip phases unless explicitly noted (e.g., false positive short-circuit).
- For each phase, clearly label which agent role you are performing as.
- Use available tools actively -- do not speculate when you can query.
- Pass context between phases: findings from Phase 1 inform Phase 2, etc.
- At the end, provide a structured summary of the entire workflow execution.
"""
        return prompt

    async def execute_workflow(
        self,
        workflow_id: str,
        parameters: Dict[str, Any],
        triggered_by: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Execute a workflow as a playbook run.

        Custom workflows with a structured ``phases`` list run phase-
        by-phase so ``approval_required`` can actually block execution
        (#128). File-based workflows without structured phases fall
        back to the legacy one-shot composite prompt — there's nothing
        to gate on.

        Returns an execution result dict. If a phase pauses on
        approval, the response shape is
        ``{success: True, status: "paused", run_id,
           pending_approval_action_id, paused_at_phase}`` and the caller
        (or the Approvals UI) must call ``resume_workflow`` once a
        decision is made.
        """
        workflow = self.get_workflow(workflow_id)
        if not workflow:
            return {"success": False, "error": f"Workflow not found: {workflow_id}"}

        phases = workflow.metadata.get("phases") or []
        if not phases:
            # No structured phases → legacy one-shot path. There's no
            # phase to gate on, so approval_required has no meaning.
            return await self._execute_oneshot(workflow, parameters, triggered_by)

        return await self._execute_phased(workflow, phases, parameters, triggered_by)

    async def resume_workflow(
        self,
        run_id: str,
        decision: str,
        *,
        rejection_reason: Optional[str] = None,
        approved_by: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Resume a paused workflow run after an approval decision (#128).

        Called from the approvals endpoint (or the workflow-run resume
        endpoint). ``decision`` is ``"approved"`` or ``"rejected"``.
        On approve, re-enters the phase loop at the paused phase. On
        reject, finalises the run as ``cancelled``.
        """
        from services.workflow_run_service import get_workflow_run_service

        if decision not in ("approved", "rejected"):
            return {"success": False, "error": f"Invalid decision: {decision}"}

        run_service = get_workflow_run_service()
        run = run_service.get_run(run_id)
        if run is None:
            return {"success": False, "error": f"Run not found: {run_id}"}
        if run.get("status") != "paused":
            return {
                "success": False,
                "error": (f"Run {run_id} is not paused (status={run.get('status')})"),
            }

        phases_rows = run_service.list_phases(run_id)
        paused = next(
            (p for p in phases_rows if p["status"] == "pending_approval"),
            None,
        )
        if paused is None:
            return {
                "success": False,
                "error": f"No pending_approval phase found on run {run_id}",
            }

        workflow = self.get_workflow(run["workflow_id"])
        if not workflow:
            run_service.finalize_run(
                run_id,
                status="failed",
                error=f"Workflow {run['workflow_id']} no longer exists",
            )
            return {
                "success": False,
                "error": f"Workflow not found: {run['workflow_id']}",
            }

        phases = workflow.metadata.get("phases") or []
        phase_index = next(
            (
                i
                for i, p in enumerate(phases)
                if (p.get("phase_id") or f"phase-{p.get('order', i + 1)}")
                == paused["phase_id"]
            ),
            None,
        )
        if phase_index is None:
            run_service.finalize_run(
                run_id,
                status="failed",
                error=(
                    f"Paused phase {paused['phase_id']} no longer in "
                    f"workflow definition"
                ),
            )
            return {
                "success": False,
                "error": "Paused phase missing from workflow definition",
            }

        if decision == "rejected":
            reason = rejection_reason or "Rejected by analyst"
            run_service.upsert_phase(
                run_id,
                paused["phase_id"],
                phase_order=paused["phase_order"],
                agent_id=paused["agent_id"],
                status="failed",
                approval_state="rejected",
                error=reason,
                finished_at=datetime.utcnow(),
            )
            run_service.finalize_run(
                run_id, status="cancelled", error=f"Rejected: {reason}"
            )
            return {
                "success": True,
                "status": "cancelled",
                "run_id": run_id,
                "rejection_reason": reason,
                "rejected_by": approved_by,
            }

        # Approved — mark the approval state and re-enter the loop.
        run_service.upsert_phase(
            run_id,
            paused["phase_id"],
            phase_order=paused["phase_order"],
            agent_id=paused["agent_id"],
            status="pending",
            approval_state="approved",
        )
        run_service.set_status(run_id, "running")

        # Rebuild accumulated context from completed prior phases.
        accumulated: Dict[str, Any] = {}
        for p in phases_rows:
            if p["status"] == "completed":
                accumulated[p["phase_id"]] = p.get("output") or {}

        return await self._run_phase_loop(
            workflow=workflow,
            phases=phases,
            start_index=phase_index,
            run_id=run_id,
            parameters=dict(run.get("trigger_context") or {}),
            accumulated=accumulated,
            triggered_by=run.get("triggered_by"),
            skill_tools_available=list(run.get("skill_tools_available") or []),
        )

    # ------------------------------------------------------------------
    # Internal execution helpers
    # ------------------------------------------------------------------

    async def _execute_oneshot(
        self,
        workflow: "WorkflowDefinition",
        parameters: Dict[str, Any],
        triggered_by: Optional[str],
    ) -> Dict[str, Any]:
        """Legacy composite-prompt path for file-based workflows that
        don't have structured phases. No approval gating possible —
        there's no phase_id to attach an approval to."""
        from services.claude_service import ClaudeService
        from services.soc_agents import SOCAgentLibrary
        from services.workflow_run_service import get_workflow_run_service

        target_context = self._build_target_context(parameters)
        all_agents = SOCAgentLibrary.get_all_agents()
        agent_profiles = {
            agent_id: all_agents[agent_id]
            for agent_id in workflow.agents
            if agent_id in all_agents
        }
        prompt = self.build_execution_prompt(
            workflow=workflow,
            target_context=target_context,
            agent_profiles=agent_profiles,
        )
        all_tools, skill_tool_names = self._collect_tools(workflow, agent_profiles)
        system_prompt = self._build_system_prompt(workflow, skill_tool_names)

        claude_service = ClaudeService(
            use_backend_tools=True,
            use_mcp_tools=True,
            use_agent_sdk=False,
            enable_thinking=True,
        )
        if not claude_service.has_api_key():
            return {"success": False, "error": "Claude API not configured"}

        workflow_dict = workflow.to_dict(include_body=False)
        run_service = get_workflow_run_service()
        run_id = run_service.begin_run(
            workflow_id=workflow.id,
            workflow_name=workflow.name,
            workflow_source=workflow_dict.get("source", "file"),
            workflow_version=workflow_dict.get("version"),
            trigger_context=dict(parameters or {}),
            triggered_by=triggered_by,
            skill_tools_available=skill_tool_names,
        )

        try:
            response_text = await asyncio.to_thread(
                claude_service.chat,
                message=prompt,
                system_prompt=system_prompt,
                model="claude-sonnet-4-5-20250929",
                max_tokens=8192,
                recommended_tools=all_tools if all_tools else None,
            )
            success = response_text is not None
            error = None if success else "Claude returned no response"
        except Exception as exc:  # noqa: BLE001
            response_text = ""
            success = False
            error = f"{type(exc).__name__}: {exc}"
            logger.exception("Workflow execution failed for %s", workflow.id)

        if run_id:
            run_service.finalize_run(
                run_id,
                status="completed" if success else "failed",
                result_summary=response_text or None,
                error=error,
            )

        return {
            "success": success,
            "status": "completed" if success else "failed",
            "run_id": run_id,
            "workflow": workflow_dict,
            "result": response_text or "",
            "tool_calls": [],
            "error": error,
            "parameters": parameters,
            "skill_tools_available": skill_tool_names,
            "executed_at": datetime.now().isoformat(),
        }

    async def _execute_phased(
        self,
        workflow: "WorkflowDefinition",
        phases: List[Dict[str, Any]],
        parameters: Dict[str, Any],
        triggered_by: Optional[str],
    ) -> Dict[str, Any]:
        """Phase-by-phase execution path for custom workflows (#128)."""
        from services.claude_service import ClaudeService
        from services.workflow_run_service import get_workflow_run_service

        if not ClaudeService(
            use_backend_tools=False, use_mcp_tools=False, use_agent_sdk=False
        ).has_api_key():
            return {"success": False, "error": "Claude API not configured"}

        _, skill_tool_names = self._collect_tools(workflow, {})

        workflow_dict = workflow.to_dict(include_body=False)
        run_service = get_workflow_run_service()
        run_id = run_service.begin_run(
            workflow_id=workflow.id,
            workflow_name=workflow.name,
            workflow_source=workflow_dict.get("source", "custom"),
            workflow_version=workflow_dict.get("version"),
            trigger_context=dict(parameters or {}),
            triggered_by=triggered_by,
            skill_tools_available=skill_tool_names,
        )

        if not run_id:
            return {
                "success": False,
                "error": "Could not persist run (DB unavailable)",
            }

        return await self._run_phase_loop(
            workflow=workflow,
            phases=phases,
            start_index=0,
            run_id=run_id,
            parameters=parameters,
            accumulated={},
            triggered_by=triggered_by,
            skill_tools_available=skill_tool_names,
        )

    async def _run_phase_loop(
        self,
        *,
        workflow: "WorkflowDefinition",
        phases: List[Dict[str, Any]],
        start_index: int,
        run_id: str,
        parameters: Dict[str, Any],
        accumulated: Dict[str, Any],
        triggered_by: Optional[str],
        skill_tools_available: List[str],
    ) -> Dict[str, Any]:
        """Shared phase-loop body used by both initial execute and
        resume. Walks phases from ``start_index``; pauses or completes
        the run as appropriate."""
        from services.claude_service import ClaudeService
        from services.soc_agents import SOCAgentLibrary
        from services.approval_service import (
            ActionType,
            get_approval_service,
        )
        from services.workflow_run_service import get_workflow_run_service

        run_service = get_workflow_run_service()
        approval_service = get_approval_service()
        all_agents = SOCAgentLibrary.get_all_agents()
        workflow_dict = workflow.to_dict(include_body=False)

        target_context = self._build_target_context(parameters)

        claude_service = ClaudeService(
            use_backend_tools=True,
            use_mcp_tools=True,
            use_agent_sdk=False,
            enable_thinking=True,
        )

        phase_outputs: List[Dict[str, Any]] = []
        last_response_text = ""

        # Existing phase rows (populated on resume) let us detect a
        # phase that was already approved and must not re-prompt.
        existing_phases = {p["phase_id"]: p for p in run_service.list_phases(run_id)}

        for idx in range(start_index, len(phases)):
            phase = phases[idx]
            phase_id = phase.get("phase_id") or f"phase-{phase.get('order', idx + 1)}"
            phase_order = int(phase.get("order", idx + 1))
            agent_id = phase.get("agent_id") or ""

            prior_row = existing_phases.get(phase_id)
            already_approved = (
                prior_row is not None
                and prior_row.get("approval_state") == "approved"
            )

            # Pre-phase approval gate (#128). Skipped if the phase row
            # already carries approval_state='approved' (resume path).
            if phase.get("approval_required") and not already_approved:
                run_service.upsert_phase(
                    run_id,
                    phase_id,
                    phase_order=phase_order,
                    agent_id=agent_id,
                    status="pending_approval",
                    input_context={"prior_outputs": accumulated},
                    approval_state="pending",
                )
                action = approval_service.create_action(
                    action_type=ActionType.WORKFLOW_PHASE,
                    title=(
                        f"Approve phase '{phase.get('name', phase_id)}' "
                        f"in {workflow.name}"
                    ),
                    description=(
                        phase.get("purpose")
                        or f"Phase {phase_order} of {workflow.name}"
                    ),
                    target=run_id,
                    confidence=0.0,
                    reason="Workflow phase marked approval_required=True",
                    evidence=[run_id],
                    created_by=triggered_by or "workflow_engine",
                    parameters={
                        "phase_id": phase_id,
                        "phase_order": phase_order,
                        "agent_id": agent_id,
                        "phase_inputs": accumulated,
                        "workflow_name": workflow.name,
                    },
                    workflow_run_id=run_id,
                    workflow_phase_id=phase_id,
                )
                run_service.set_status(run_id, "paused")
                return {
                    "success": True,
                    "status": "paused",
                    "run_id": run_id,
                    "workflow": workflow_dict,
                    "pending_approval_action_id": action.action_id,
                    "paused_at_phase": phase_id,
                    "parameters": parameters,
                    "skill_tools_available": skill_tools_available,
                    "executed_at": datetime.now().isoformat(),
                }

            # Run the phase.
            run_service.upsert_phase(
                run_id,
                phase_id,
                phase_order=phase_order,
                agent_id=agent_id,
                status="running",
                input_context={"prior_outputs": accumulated},
                started_at=datetime.utcnow(),
            )

            profile = all_agents.get(agent_id)
            phase_prompt = self._build_phase_prompt(
                workflow=workflow,
                phase=phase,
                target_context=target_context,
                prior_outputs=accumulated,
            )
            system_prompt = self._build_system_prompt(
                workflow, skill_tools_available, single_phase=phase
            )
            phase_tools = self._tools_for_phase(phase, profile, skill_tools_available)

            try:
                response_text = await asyncio.to_thread(
                    claude_service.chat,
                    message=phase_prompt,
                    system_prompt=system_prompt,
                    model="claude-sonnet-4-5-20250929",
                    max_tokens=8192,
                    recommended_tools=phase_tools or None,
                )
                phase_ok = response_text is not None
                phase_error = None if phase_ok else "Claude returned no response"
            except Exception as exc:  # noqa: BLE001
                response_text = ""
                phase_ok = False
                phase_error = f"{type(exc).__name__}: {exc}"
                logger.exception(
                    "Workflow phase %s failed for run %s", phase_id, run_id
                )

            finished = datetime.utcnow()
            if phase_ok:
                output = {"text": response_text or ""}
                run_service.upsert_phase(
                    run_id,
                    phase_id,
                    phase_order=phase_order,
                    agent_id=agent_id,
                    status="completed",
                    output=output,
                    finished_at=finished,
                )
                accumulated[phase_id] = output
                phase_outputs.append(
                    {"phase_id": phase_id, "output": response_text or ""}
                )
                last_response_text = response_text or last_response_text
            else:
                run_service.upsert_phase(
                    run_id,
                    phase_id,
                    phase_order=phase_order,
                    agent_id=agent_id,
                    status="failed",
                    error=phase_error,
                    finished_at=finished,
                )
                run_service.finalize_run(
                    run_id,
                    status="failed",
                    result_summary=self._combine_summary(phase_outputs),
                    error=phase_error,
                )
                return {
                    "success": False,
                    "status": "failed",
                    "run_id": run_id,
                    "workflow": workflow_dict,
                    "result": self._combine_summary(phase_outputs),
                    "tool_calls": [],
                    "error": phase_error,
                    "parameters": parameters,
                    "skill_tools_available": skill_tools_available,
                    "executed_at": datetime.now().isoformat(),
                }

        summary = self._combine_summary(phase_outputs) or last_response_text
        run_service.finalize_run(
            run_id,
            status="completed",
            result_summary=summary or None,
        )
        return {
            "success": True,
            "status": "completed",
            "run_id": run_id,
            "workflow": workflow_dict,
            "result": summary or "",
            "tool_calls": [],
            "error": None,
            "parameters": parameters,
            "skill_tools_available": skill_tools_available,
            "executed_at": datetime.now().isoformat(),
        }

    # ------------------------------------------------------------------
    # Prompt / tool helpers
    # ------------------------------------------------------------------

    def _collect_tools(
        self,
        workflow: "WorkflowDefinition",
        agent_profiles: Dict[str, Any],
    ) -> tuple[List[str], List[str]]:
        """Collect workflow + agent + MCP + skill tools. Returns
        ``(all_tools, skill_tool_names)``."""
        all_tools = list(workflow.tools_used)
        for agent_id in workflow.agents:
            profile = agent_profiles.get(agent_id)
            if profile and getattr(profile, "recommended_tools", None):
                for tool in profile.recommended_tools:
                    if tool not in all_tools:
                        all_tools.append(tool)
        try:
            from services.mcp_registry import get_mcp_registry

            registry = get_mcp_registry()
            for name in registry.get_tool_names() or []:
                if name not in all_tools:
                    all_tools.append(name)
        except Exception as e:  # noqa: BLE001
            logger.debug("Could not get MCP tools from registry: %s", e)

        skill_tool_names: List[str] = []
        try:
            from services.skill_tools_bridge import list_active_skill_tools

            skill_defs, _ = list_active_skill_tools()
            skill_tool_names = [t["name"] for t in skill_defs]
            for name in skill_tool_names:
                if name not in all_tools:
                    all_tools.append(name)
        except Exception as e:  # noqa: BLE001
            logger.debug("Could not load active skill tools: %s", e)

        return all_tools, skill_tool_names

    def _tools_for_phase(
        self,
        phase: Dict[str, Any],
        profile: Optional[Any],
        skill_tool_names: List[str],
    ) -> List[str]:
        """Narrow the tool list to what this phase actually needs."""
        tools = list(phase.get("tools") or [])
        if profile and getattr(profile, "recommended_tools", None):
            for t in profile.recommended_tools:
                if t not in tools:
                    tools.append(t)
        try:
            from services.mcp_registry import get_mcp_registry

            registry = get_mcp_registry()
            for name in registry.get_tool_names() or []:
                if name not in tools:
                    tools.append(name)
        except Exception:  # noqa: BLE001
            pass
        for name in skill_tool_names:
            if name not in tools:
                tools.append(name)
        return tools

    def _build_system_prompt(
        self,
        workflow: "WorkflowDefinition",
        skill_tool_names: List[str],
        single_phase: Optional[Dict[str, Any]] = None,
    ) -> str:
        """System prompt shared by oneshot and per-phase execution."""
        skills_hint = ""
        if skill_tool_names:
            skills_hint = (
                "\n<available_skills>\n"
                "The following skill tools are available as reusable SOC "
                "capabilities. Invoke by name whenever the phase's work "
                "matches a skill's purpose — each call returns the "
                "skill's rendered playbook text for you to act on.\n"
                + "\n".join(f"- {name}" for name in skill_tool_names)
                + "\n</available_skills>\n"
            )
        scope = (
            f'phase "{single_phase.get("name", single_phase.get("phase_id"))}"'
            if single_phase
            else "multi-phase workflow"
        )
        header = (
            f'You are the Vigil SOC Workflow Engine executing the '
            f'"{workflow.name}" {scope}.'
        )
        return f"""{header}

You have access to SOC tools and must ground every conclusion in tool output.

<entity_recognition>
- Finding IDs (f-YYYYMMDD-XXXXXXXX): Use get_finding tool
- Case IDs (case-YYYYMMDD-XXXXXXXX): Use get_case tool
- IPs/domains/hashes: Use threat intel tools
- NEVER access findings as files - use tools
</entity_recognition>
{skills_hint}
<principles>
- Always fetch data via tools before analyzing
- Be evidence-based and document reasoning
- Use parallel tool calls for independent queries
- Return a concise, structured summary suitable as input to the next phase
</principles>
"""

    def _build_phase_prompt(
        self,
        workflow: "WorkflowDefinition",
        phase: Dict[str, Any],
        target_context: str,
        prior_outputs: Dict[str, Any],
    ) -> str:
        """Focused prompt for a single phase. Includes accumulated
        outputs from prior phases so context carries forward."""
        lines: List[str] = [
            f"# Phase {phase.get('order', '?')}: {phase.get('name', '')}",
            "",
            f"**Workflow:** {workflow.name}",
            f"**Agent role:** {phase.get('agent_id', '')}",
        ]
        if phase.get("purpose"):
            lines += ["", f"**Purpose:** {phase['purpose']}"]
        lines += ["", "## Target Context", target_context]
        if prior_outputs:
            lines += ["", "## Prior Phase Outputs"]
            for pid, out in prior_outputs.items():
                text = (out or {}).get("text") if isinstance(out, dict) else str(out)
                if text:
                    lines += [f"### {pid}", text.strip()]
        steps = phase.get("steps") or []
        if steps:
            lines += ["", "## Steps"]
            for i, step in enumerate(steps, start=1):
                lines.append(f"{i}. {step}")
        if phase.get("expected_output"):
            lines += ["", f"**Expected output:** {phase['expected_output']}"]
        lines += [
            "",
            "Execute this phase using the tools available, grounding "
            "every claim in tool results. Conclude with a structured "
            "summary suitable as input for the next phase.",
        ]
        return "\n".join(lines)

    def _combine_summary(self, phase_outputs: List[Dict[str, Any]]) -> str:
        """Concatenate per-phase outputs into a single run summary."""
        parts: List[str] = []
        for p in phase_outputs:
            parts.append(f"### {p['phase_id']}\n{p.get('output', '')}")
        return "\n\n".join(parts)

    def _build_target_context(self, parameters: Dict[str, Any]) -> str:
        """Build a context string from execution parameters."""
        parts = []

        finding_id = parameters.get("finding_id")
        case_id = parameters.get("case_id")
        context = parameters.get("context", "")
        hypothesis = parameters.get("hypothesis", "")

        if finding_id:
            try:
                from services.database_data_service import DatabaseDataService

                data_service = DatabaseDataService()
                finding = data_service.get_finding(finding_id)
                if finding:
                    techniques = finding.get("predicted_techniques", [])
                    technique_str = (
                        ", ".join([t.get("technique_id", "") for t in techniques])
                        if techniques
                        else "None"
                    )
                    parts.append(f"""**Target Finding:**
- Finding ID: {finding.get('finding_id')}
- Severity: {finding.get('severity')}
- Data Source: {finding.get('data_source')}
- Timestamp: {finding.get('timestamp')}
- Anomaly Score: {finding.get('anomaly_score', 'N/A')}
- Description: {finding.get('description', 'N/A')}
- MITRE ATT&CK Techniques: {technique_str}""")
                else:
                    parts.append(
                        f"**Target Finding ID:** {finding_id} (details will be retrieved during execution)"
                    )
            except Exception:
                parts.append(
                    f"**Target Finding ID:** {finding_id} (use get_finding to retrieve details)"
                )

        if case_id:
            try:
                from services.database_data_service import DatabaseDataService

                data_service = DatabaseDataService()
                case = data_service.get_case(case_id)
                if case:
                    parts.append(f"""**Target Case:**
- Case ID: {case.get('case_id')}
- Title: {case.get('title')}
- Status: {case.get('status')}
- Priority: {case.get('priority')}
- Description: {case.get('description', 'N/A')}
- Finding Count: {len(case.get('finding_ids', []))}""")
                else:
                    parts.append(
                        f"**Target Case ID:** {case_id} (details will be retrieved during execution)"
                    )
            except Exception:
                parts.append(
                    f"**Target Case ID:** {case_id} (use get_case to retrieve details)"
                )

        if hypothesis:
            parts.append(f"**Hunt Hypothesis:** {hypothesis}")

        if context:
            parts.append(f"**Additional Context:** {context}")

        if not parts:
            parts.append(
                "No specific target provided. Use available tools to identify relevant findings and cases."
            )

        return "\n\n".join(parts)


# Singleton instance
_workflows_service: Optional[WorkflowsService] = None


def get_workflows_service() -> WorkflowsService:
    """Get singleton WorkflowsService instance."""
    global _workflows_service
    if _workflows_service is None:
        _workflows_service = WorkflowsService()
    return _workflows_service
