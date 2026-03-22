"""Workflows service for discovering, parsing, and executing WORKFLOW.md workflow definitions."""

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
    match = re.match(r'^---\s*\n(.*?)\n---\s*(?:\n|$)', content, re.DOTALL)
    if not match:
        return {}

    frontmatter_text = match.group(1)
    result = {}
    current_key = None
    current_list = None

    for line in frontmatter_text.split('\n'):
        # Skip empty lines and comments
        stripped = line.strip()
        if not stripped or stripped.startswith('#'):
            continue

        # Check for list continuation (indented "- item")
        if current_key and current_list is not None and re.match(r'^\s+-\s+', line):
            item = re.sub(r'^\s+-\s+', '', line).strip().strip('"').strip("'")
            current_list.append(item)
            result[current_key] = current_list
            continue

        # Key-value pair
        kv_match = re.match(r'^(\S+):\s*(.*)', line)
        if kv_match:
            key = kv_match.group(1)
            value = kv_match.group(2).strip()
            current_key = key
            current_list = None

            if not value:
                # Might be start of a list
                current_list = []
                result[key] = current_list
            elif value.startswith('[') and value.endswith(']'):
                # Inline list: [item1, item2, ...]
                items = value[1:-1].split(',')
                result[key] = [i.strip().strip('"').strip("'") for i in items if i.strip()]
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
    match = re.match(r'^---\s*\n(.*?)\n---\s*(?:\n|$)', content, re.DOTALL)
    if match:
        return match.end()
    return 0


class WorkflowDefinition:
    """Represents a parsed workflow from a WORKFLOW.md file."""

    def __init__(self, workflow_id: str, file_path: Path, metadata: Dict[str, Any], body: str):
        self.id = workflow_id
        self.file_path = file_path
        self.metadata = metadata
        self.body = body

    @property
    def name(self) -> str:
        return self.metadata.get('name', self.id)

    @property
    def description(self) -> str:
        return self.metadata.get('description', '')

    @property
    def agents(self) -> List[str]:
        agents = self.metadata.get('agents', [])
        if isinstance(agents, str):
            return [a.strip() for a in agents.split(',')]
        return agents

    @property
    def tools_used(self) -> List[str]:
        tools = self.metadata.get('tools-used', [])
        if isinstance(tools, str):
            return [t.strip() for t in tools.split(',')]
        return tools

    @property
    def use_case(self) -> str:
        return self.metadata.get('use-case', '')

    @property
    def trigger_examples(self) -> List[str]:
        examples = self.metadata.get('trigger-examples', [])
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
        }
        if include_body:
            result["body"] = self.body
        return result


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
                content = workflow_file.read_text(encoding='utf-8')
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

    def list_workflows(self) -> List[Dict[str, Any]]:
        """Return metadata for all discovered workflows."""
        return [workflow.to_dict(include_body=False) for workflow in self._cache.values()]

    def get_workflow(self, workflow_id: str) -> Optional[WorkflowDefinition]:
        """Get a specific workflow by ID."""
        return self._cache.get(workflow_id)

    def get_workflow_dict(self, workflow_id: str, include_body: bool = True) -> Optional[Dict[str, Any]]:
        """Get a specific workflow as a dictionary."""
        workflow = self._cache.get(workflow_id)
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
            agent_section += "Here are the methodologies for each agent you will use:\n\n"

            for agent_id in workflow.agents:
                profile = agent_profiles.get(agent_id)
                if profile:
                    agent_section += f"### {profile.name} ({agent_id})\n"
                    agent_section += f"**Specialization:** {profile.specialization}\n"
                    agent_section += f"**Description:** {profile.description}\n"
                    # Extract methodology from system prompt
                    methodology_match = re.search(
                        r'<methodology>(.*?)</methodology>',
                        profile.system_prompt,
                        re.DOTALL
                    )
                    if methodology_match:
                        agent_section += f"**Methodology:**\n{methodology_match.group(1).strip()}\n"
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
    ) -> Dict[str, Any]:
        """
        Execute a workflow by building a composite prompt and running it
        through ClaudeService.run_agent_task().

        Args:
            workflow_id: The workflow ID to execute
            parameters: Execution parameters:
                - finding_id: Optional finding to investigate
                - case_id: Optional case to investigate
                - context: Optional freeform context string
                - hypothesis: Optional hunt hypothesis (for threat-hunt)

        Returns:
            Execution result dict
        """
        from services.claude_service import ClaudeService
        from services.soc_agents import SOCAgentLibrary

        workflow = self.get_workflow(workflow_id)
        if not workflow:
            return {"success": False, "error": f"Workflow not found: {workflow_id}"}

        # Build target context from parameters
        target_context = self._build_target_context(parameters)

        # Get agent profiles for methodology embedding
        all_agents = SOCAgentLibrary.get_all_agents()
        agent_profiles = {
            agent_id: all_agents[agent_id]
            for agent_id in workflow.agents
            if agent_id in all_agents
        }

        # Build the composite execution prompt
        prompt = self.build_execution_prompt(
            workflow=workflow,
            target_context=target_context,
            agent_profiles=agent_profiles,
        )

        # Collect all tools needed across all agents in the workflow
        all_tools = list(workflow.tools_used)
        for agent_id in workflow.agents:
            profile = agent_profiles.get(agent_id)
            if profile and profile.recommended_tools:
                for tool in profile.recommended_tools:
                    if tool not in all_tools:
                        all_tools.append(tool)

        # Add MCP tools if available
        try:
            from services.mcp_registry import get_mcp_registry
            registry = get_mcp_registry()
            mcp_tool_names = registry.get_tool_names()
            if mcp_tool_names:
                all_tools.extend(mcp_tool_names)
        except Exception as e:
            logger.debug(f"Could not get MCP tools from registry: {e}")

        # Build a composite system prompt incorporating all agent roles
        system_prompt = f"""You are the Vigil SOC Workflow Engine executing the "{workflow.name}" workflow.

You have access to all SOC tools and will execute a multi-phase workflow,
adopting different specialist agent roles for each phase.

<entity_recognition>
- Finding IDs (f-YYYYMMDD-XXXXXXXX): Use get_finding tool
- Case IDs (case-YYYYMMDD-XXXXXXXX): Use get_case tool
- IPs/domains/hashes: Use threat intel tools
- NEVER access findings as files - use tools
</entity_recognition>

<principles>
- Always fetch data via tools before analyzing
- Be evidence-based and document reasoning
- Use parallel tool calls for independent queries
- Follow each workflow phase in sequence
- Pass context between phases
</principles>
"""

        # Execute via ClaudeService
        claude_service = ClaudeService(
            use_backend_tools=True,
            use_mcp_tools=True,
            use_agent_sdk=True,
            enable_thinking=True,
        )

        if not claude_service.has_api_key():
            return {"success": False, "error": "Claude API not configured"}

        result = await claude_service.run_agent_task(
            task=prompt,
            agent_config={
                "system_prompt": system_prompt,
                "allowed_tools": all_tools if all_tools else None,
                "max_turns": 25,  # More turns for multi-phase workflows
                "model": "claude-sonnet-4-5-20250929",
            }
        )

        return {
            "success": result.get("success", False),
            "workflow": workflow.to_dict(include_body=False),
            "result": result.get("final_result", ""),
            "tool_calls": result.get("tool_calls", []),
            "error": result.get("error"),
            "parameters": parameters,
            "executed_at": datetime.now().isoformat(),
        }

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
                    techniques = finding.get('predicted_techniques', [])
                    technique_str = ', '.join([t.get('technique_id', '') for t in techniques]) if techniques else 'None'
                    parts.append(f"""**Target Finding:**
- Finding ID: {finding.get('finding_id')}
- Severity: {finding.get('severity')}
- Data Source: {finding.get('data_source')}
- Timestamp: {finding.get('timestamp')}
- Anomaly Score: {finding.get('anomaly_score', 'N/A')}
- Description: {finding.get('description', 'N/A')}
- MITRE ATT&CK Techniques: {technique_str}""")
                else:
                    parts.append(f"**Target Finding ID:** {finding_id} (details will be retrieved during execution)")
            except Exception:
                parts.append(f"**Target Finding ID:** {finding_id} (use get_finding to retrieve details)")

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
                    parts.append(f"**Target Case ID:** {case_id} (details will be retrieved during execution)")
            except Exception:
                parts.append(f"**Target Case ID:** {case_id} (use get_case to retrieve details)")

        if hypothesis:
            parts.append(f"**Hunt Hypothesis:** {hypothesis}")

        if context:
            parts.append(f"**Additional Context:** {context}")

        if not parts:
            parts.append("No specific target provided. Use available tools to identify relevant findings and cases.")

        return "\n\n".join(parts)


# Singleton instance
_workflows_service: Optional[WorkflowsService] = None


def get_workflows_service() -> WorkflowsService:
    """Get singleton WorkflowsService instance."""
    global _workflows_service
    if _workflows_service is None:
        _workflows_service = WorkflowsService()
    return _workflows_service
