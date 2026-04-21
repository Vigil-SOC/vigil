"""Agents API endpoints for SOC agent management."""

from typing import Optional
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
import logging

from services.soc_agents import SOCAgentLibrary, AgentManager, CUSTOM_AGENT_ID_PREFIX

router = APIRouter()
logger = logging.getLogger(__name__)

# Global agent manager instance
agent_manager = AgentManager()


def _resolve_agent(agent_id: str):
    """Resolve an agent_id to an AgentProfile, lazy-loading custom agents on miss.

    Built-in agents are served from the in-memory dict with zero DB calls.
    Only misses for IDs prefixed with custom- trigger a refresh and retry.
    """
    agent = agent_manager.agents.get(agent_id)
    if agent is not None:
        return agent
    if agent_id and agent_id.startswith(CUSTOM_AGENT_ID_PREFIX):
        agent_manager.refresh_custom_agents()
        return agent_manager.agents.get(agent_id)
    return None


class InvestigationRequest(BaseModel):
    """Request to start an investigation with an agent."""
    finding_id: str
    agent_id: Optional[str] = "investigator"
    additional_context: Optional[str] = None


@router.get("/agents")
async def list_agents():
    """
    Get list of all available SOC agents.
    
    Returns:
        List of agents with their metadata
    """
    try:
        agents = agent_manager.get_agent_list()
        return {
            "agents": agents,
            "current_agent": agent_manager.current_agent_id
        }
    except Exception as e:
        logger.error(f"Error listing agents: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/agents/{agent_id}")
async def get_agent(agent_id: str):
    """
    Get details for a specific agent.
    
    Args:
        agent_id: The agent ID
    
    Returns:
        Agent details
    """
    try:
        agent = _resolve_agent(agent_id)
        if not agent:
            raise HTTPException(status_code=404, detail=f"Agent not found: {agent_id}")

        return {
            "id": agent.id,
            "name": agent.name,
            "description": agent.description,
            "icon": agent.icon,
            "color": agent.color,
            "specialization": agent.specialization,
            "recommended_tools": agent.recommended_tools,
            "max_tokens": agent.max_tokens,
            "enable_thinking": agent.enable_thinking
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting agent: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/agents/set-current")
async def set_current_agent(agent_id: str):
    """
    Set the current active agent.
    
    Args:
        agent_id: The agent ID to set as current
    
    Returns:
        Success status
    """
    try:
        success = agent_manager.set_current_agent(agent_id)
        if not success:
            raise HTTPException(status_code=404, detail=f"Agent not found: {agent_id}")
        
        return {
            "success": True,
            "current_agent": agent_manager.current_agent_id
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error setting current agent: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/agents/investigate")
async def start_investigation(request: InvestigationRequest):
    """
    Start an investigation on a finding with a specific agent.
    
    Args:
        request: Investigation request with finding ID and agent
    
    Returns:
        Investigation prompt and agent details
    """
    from services.database_data_service import DatabaseDataService
    
    try:
        # Get the finding
        data_service = DatabaseDataService()
        finding = data_service.get_finding(request.finding_id)
        
        if not finding:
            raise HTTPException(status_code=404, detail=f"Finding not found: {request.finding_id}")
        
        # Get the agent
        agent = _resolve_agent(request.agent_id)
        if not agent:
            raise HTTPException(status_code=404, detail=f"Agent not found: {request.agent_id}")
        
        # Construct investigation prompt
        techniques = finding.get('predicted_techniques', [])
        technique_str = ', '.join([t.get('technique_id', '') for t in techniques]) if techniques else 'None'
        
        prompt = f"""Please investigate this security finding:

**Finding ID:** {finding.get('finding_id')}
**Severity:** {finding.get('severity')}
**Data Source:** {finding.get('data_source')}
**Timestamp:** {finding.get('timestamp')}
**Anomaly Score:** {finding.get('anomaly_score', 'N/A')}
**Description:** {finding.get('description', 'N/A')}
**Predicted MITRE ATT&CK Techniques:** {technique_str}

{f'**Additional Context:** {request.additional_context}' if request.additional_context else ''}

Please conduct a thorough investigation of this finding. Use your available tools to gather more information, correlate with other findings, and provide your analysis."""
        
        return {
            "prompt": prompt,
            "agent": {
                "id": agent.id,
                "name": agent.name,
                "icon": agent.icon,
                "color": agent.color,
                "system_prompt": agent.system_prompt
            },
            "finding": finding
        }
    
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error starting investigation: {e}")
        raise HTTPException(status_code=500, detail=str(e))


class AgentRunRequest(BaseModel):
    """Request to run an agent task with Agent SDK."""
    finding_id: Optional[str] = None
    case_id: Optional[str] = None
    task: Optional[str] = None
    agent_id: str = "investigator"
    use_agent_sdk: bool = True


@router.post("/agents/run")
async def run_agent(request: AgentRunRequest):
    """
    Run an agent task using Claude Agent SDK.
    
    This endpoint leverages the Agent SDK for autonomous tool execution,
    allowing the agent to investigate findings, cases, or perform custom tasks.
    
    Args:
        request: Agent run request
    
    Returns:
        Agent execution result with tool calls and analysis
    """
    from services.database_data_service import DatabaseDataService
    from services.claude_service import ClaudeService
    
    try:
        agent = _resolve_agent(request.agent_id)
        if not agent:
            raise HTTPException(status_code=404, detail=f"Agent not found: {request.agent_id}")

        # Build task from finding/case if not explicitly provided
        task = request.task
        context_data = {}
        
        if request.finding_id and not task:
            data_service = DatabaseDataService()
            finding = data_service.get_finding(request.finding_id)
            if not finding:
                raise HTTPException(status_code=404, detail=f"Finding not found: {request.finding_id}")
            
            techniques = finding.get('predicted_techniques', [])
            technique_str = ', '.join([t.get('technique_id', '') for t in techniques]) if techniques else 'None'
            
            task = f"""Investigate security finding {finding.get('finding_id')}.

Finding Details:
- Severity: {finding.get('severity')}
- Data Source: {finding.get('data_source')}
- Timestamp: {finding.get('timestamp')}
- Anomaly Score: {finding.get('anomaly_score', 'N/A')}
- Description: {finding.get('description', 'N/A')}
- MITRE ATT&CK Techniques: {technique_str}

Use the get_finding tool first to retrieve full details, then investigate thoroughly."""
            context_data['finding'] = finding
            
        elif request.case_id and not task:
            data_service = DatabaseDataService()
            case = data_service.get_case(request.case_id)
            if not case:
                raise HTTPException(status_code=404, detail=f"Case not found: {request.case_id}")
            
            task = f"""Investigate case {case.get('case_id')}.

Case Details:
- Title: {case.get('title')}
- Status: {case.get('status')}
- Priority: {case.get('priority')}
- Description: {case.get('description', 'N/A')}
- Finding Count: {len(case.get('finding_ids', []))}

Use the get_case tool first to retrieve full details, then investigate all associated findings."""
            context_data['case'] = case
        
        if not task:
            raise HTTPException(status_code=400, detail="No task, finding_id, or case_id provided")
        
        claude_service = ClaudeService(
            use_backend_tools=True,
            use_mcp_tools=True,  # Enable MCP tools for dynamic enrichment
            use_agent_sdk=request.use_agent_sdk,
            enable_thinking=agent.enable_thinking
        )
        
        if not claude_service.has_api_key():
            raise HTTPException(status_code=503, detail="Claude API not configured")
        
        # Build allowed tools: combine agent's recommended tools with
        # dynamically discovered MCP tools from the registry
        allowed_tools = list(agent.recommended_tools) if agent.recommended_tools else []
        try:
            from services.mcp_registry import get_mcp_registry
            registry = get_mcp_registry()
            mcp_tool_names = registry.get_tool_names()
            if mcp_tool_names:
                allowed_tools.extend(mcp_tool_names)
                logger.info(f"Agent '{agent.id}' enriched with {len(mcp_tool_names)} MCP tools")
        except Exception as e:
            logger.debug(f"Could not get MCP tools from registry: {e}")
        
        result = await claude_service.run_agent_task(
            task=task,
            agent_config={
                "system_prompt": agent.system_prompt,
                "allowed_tools": allowed_tools if allowed_tools else None,
                "max_turns": 15,
                "model": "claude-sonnet-4-5-20250929"
            }
        )
        
        return {
            "success": result.get("success", False),
            "agent": {
                "id": agent.id,
                "name": agent.name,
                "icon": agent.icon,
                "color": agent.color
            },
            "task": task,
            "result": result.get("final_result", ""),
            "tool_calls": result.get("tool_calls", []),
            "error": result.get("error"),
            "context": context_data,
            "agent_sdk_used": request.use_agent_sdk
        }
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error running agent: {e}")
        raise HTTPException(status_code=500, detail=str(e))

