"""API package initialization."""

from api.findings import router as findings_router
from api.cases import router as cases_router
from api.mcp import router as mcp_router
from api.claude import router as claude_router
from api.config import router as config_router
from api.attack import router as attack_router
from api.agents import router as agents_router
from api.custom_integrations import router as custom_integrations_router
from api.ingestion import router as ingestion_router
from api.storage_status import router as storage_status_router
from api.ai_decisions import router as ai_decisions_router
from api.logs import router as logs_router
from api.workflows import router as workflows_router
from api.reasoning import router as reasoning_router
from api.skills import router as skills_router
from api.llm_providers import router as llm_providers_router

__all__ = [
    'findings_router',
    'cases_router',
    'mcp_router',
    'claude_router',
    'config_router',
    'attack_router',
    'agents_router',
    'custom_integrations_router',
    'ingestion_router',
    'storage_status_router',
    'ai_decisions_router',
    'logs_router',
    'workflows_router',
    'reasoning_router',
    'skills_router',
    'llm_providers_router',
]
