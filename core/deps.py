"""Depends providers for the services the API builds once at startup.

``services/api/main.py``'s lifespan constructs each service onto ``app.state``;
handlers reach them through the providers here. Each provider is a module-level
function object on purpose: ``app.dependency_overrides`` keys on identity, so a
factory that minted a new closure per call could never be overridden in a test.

This module lives in ``core/`` rather than ``services/api/`` because routers
colocate with their domain (``core/<domain>/<name>_router.py``) and ``core`` must
not import ``services`` — see ``.importlinter``.
"""

from typing import Optional

from fastapi import Request

from core.agents.agent_ai_generator import AgentAIGenerator
from core.detections.detection_rules_service import DetectionRulesService
from core.integrations.integration_bridge_service import IntegrationBridgeService
from core.integrations.mcp.registry import MCPRegistry
from core.platform.demo_data_service import DemoDataService
from core.response.approval_service import ApprovalService
from core.workflows.custom_workflow_service import CustomWorkflowService
from core.workflows.workflow_ai_generator import WorkflowAIGenerator
from core.workflows.workflow_run_service import WorkflowRunService
from core.workflows.workflows_service import WorkflowsService


def provide_approvals(request: Request) -> ApprovalService:
    return request.app.state.approvals


def provide_workflows(request: Request) -> WorkflowsService:
    return request.app.state.workflows


def provide_custom_workflows(request: Request) -> CustomWorkflowService:
    return request.app.state.custom_workflows


def provide_workflow_runs(request: Request) -> WorkflowRunService:
    return request.app.state.workflow_runs


def provide_workflow_ai(request: Request) -> WorkflowAIGenerator:
    return request.app.state.workflow_ai


def provide_agent_ai(request: Request) -> AgentAIGenerator:
    return request.app.state.agent_ai


def provide_mcp_registry(request: Request) -> MCPRegistry:
    return request.app.state.mcp_registry


def provide_integration_bridge(request: Request) -> IntegrationBridgeService:
    return request.app.state.integration_bridge


def provide_detection_rules(request: Request) -> DetectionRulesService:
    return request.app.state.detection_rules


# None when the MCP SDK is not installed.
def provide_mcp_client(request: Request):
    return request.app.state.mcp_client


# None unless demo mode is enabled. Demo mode is toggleable at runtime via
# POST /api/config/demo-mode, so this resolves per request rather than trusting the
# startup snapshot; DemoDataService shares one instance either way.
def provide_demo_data(request: Request) -> Optional[DemoDataService]:
    from core.config import is_demo_mode

    if not is_demo_mode():
        return None
    return request.app.state.demo_data or DemoDataService()
