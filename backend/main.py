import json
import logging
import os
import sys
from contextlib import asynccontextmanager
from pathlib import Path

# Add project root and backend directories to Python path for imports
project_root = str(Path(__file__).parent.parent)
backend_dir = str(Path(__file__).parent)
if project_root not in sys.path:
    sys.path.insert(0, project_root)
if backend_dir not in sys.path:
    sys.path.insert(0, backend_dir)

from fastapi import FastAPI, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from slowapi.errors import RateLimitExceeded
from slowapi import _rate_limit_exceeded_handler

from backend import __version__
from backend.middleware.csrf import CSRFMiddleware
from backend.middleware.rate_limit import limiter
from backend.middleware.security_headers import SecurityHeadersMiddleware

from api import (
    findings_router,
    cases_router,
    mcp_router,
    claude_router,
    config_router,
    attack_router,
    agents_router,
    custom_integrations_router,
    storage_status_router,
    ai_decisions_router,
    logs_router,
    workflows_router,
    approvals_router,
    reasoning_router,
    conversations_router,
    skills_router,
    llm_providers_router,
    ai_config_router,
)
from api.local_services import router as local_services_router
from api.integrations_compatibility import router as compatibility_router
from api.ingestion import router as ingestion_router
from api.timeline import router as timeline_router
from api.graph import router as graph_router
from api.vstrike import router as vstrike_router
from api.extensions import router as extensions_router
from api.custom_agents import router as custom_agents_router

# Enhanced case management routers
from api.case_templates import router as case_templates_router
from api.case_metrics import router as case_metrics_router
from api.case_search import router as case_search_router
from api.webhooks import router as webhooks_router
from api.sla_policies import router as sla_policies_router

# Darktrace inbound webhook receiver
from api.darktrace_webhook import router as darktrace_webhook_router

# Cloudflare Cloudy inbound webhook receiver (gated; see CLOUDY_INGESTION_ENABLED)
from api.cloudflare_webhooks import (
    router as cloudflare_webhooks_router,
    cloudy_ingestion_enabled,
)

# Authentication routers
from api.auth import router as auth_router
from api.users import router as users_router

# JIRA export router
from api.jira_export import router as jira_export_router

# Analytics router
from api.analytics import router as analytics_router

# Detection Rules router
from api.detection_rules import router as detection_rules_router

# Orchestrator router
from api.orchestrator import router as orchestrator_router

# Federation router (federated monitoring of external SIEM/EDR sources)
from api.federation import router as federation_router

# Budgets (Bifrost VK config + live quota — #186)
from api.budgets import router as budgets_router

# Kafka ingestion router
from api.kafka import router as kafka_router

from core.config import get_settings
from core.rate_limit import rate_limit_dependency
from backend.middleware.auth import get_current_active_user
from monitoring import init_sentry, PROMETHEUS_AVAILABLE, get_metrics_response

# Single source of truth for the "require an authenticated active user"
# dependency. Applied to every non-public /api/* router below so that any
# new endpoint added under a protected router inherits auth by default.
# Endpoints that must stay public (auth, inbound webhooks, health) are
# either left off the dependency or listed in ``PUBLIC_API_PATHS`` so the
# route-inventory test in ``tests/security/test_route_auth_coverage.py``
# still passes.
AUTH_DEPENDENCY = [Depends(get_current_active_user)]


# Intentionally-public routes. The route-inventory test asserts every
# /api/* route either inherits AUTH_DEPENDENCY or is listed here.
# Patterns ending in '/*' match any sub-path.
PUBLIC_API_PATHS: frozenset[str] = frozenset(
    {
        "/api/auth/login",
        "/api/auth/refresh",
        "/api/auth/logout",
        "/api/auth/password-reset/request",
        "/api/auth/password-reset/confirm",
        # First-run account creation — unauthenticated by necessity; only ever
        # live on an empty instance (see backend/api/auth.py bootstrap routes).
        "/api/auth/bootstrap",
        # Health check — used by load balancers and Docker.
        "/api/health",
        # VStrike inbound receiver uses its own bearer API-key dependency.
        "/api/integrations/vstrike/findings",
    }
)

if PROMETHEUS_AVAILABLE:
    from monitoring import PrometheusMiddleware

# Initialize telemetry before creating the FastAPI app so instrumentation
# is registered before the first request handler is defined.
try:
    from core.telemetry import init_telemetry

    init_telemetry("vigil-backend")
except Exception as _tel_err:
    logging.basicConfig(level=logging.INFO)
    logging.getLogger(__name__).warning(
        "Telemetry init failed (non-fatal): %s", _tel_err
    )

logger = logging.getLogger(__name__)

# Initialize Sentry as early as possible (no-op if SENTRY_DSN is unset)
init_sentry()


# Delegates to _startup/_shutdown, defined further down alongside the rest of the
# startup logic; they resolve at call time so the ordering here is fine.
@asynccontextmanager
async def lifespan(app: FastAPI):
    await _startup(app)
    try:
        yield
    finally:
        await _shutdown(app)


# Create FastAPI app
app = FastAPI(
    title="Vigil SOC API",
    description="REST API for Vigil SOC Application",
    version=__version__,
    lifespan=lifespan,
)

# Optional context path (sub-path) the whole app is served under, e.g. when
# Vigil sits behind a reverse proxy at https://host/vigil. Empty by default
# (served at root). All API routers, the health endpoint, the static/assets
# mounts and the SPA catch-all are prefixed with this; the frontend learns it
# at runtime via the <meta name="vigil-base-path"> injected into index.html below.
_CONTEXT_PATH = get_settings().vigil_context_path.rstrip("/")

# Wire the shared slowapi Limiter used by auth endpoints. The decorator-based
# limits (@limiter.limit) read state from app.state.limiter, so both must be set.
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

# Instrument FastAPI with OTEL tracing (health + metrics endpoints excluded)
try:
    from opentelemetry.instrumentation.fastapi import FastAPIInstrumentation

    FastAPIInstrumentation().instrument_app(
        app,
        excluded_urls="api/health,metrics",
    )
except Exception as _inst_err:
    logger.debug("FastAPI OTEL instrumentation skipped: %s", _inst_err)

# Configure CORS — origins come from VIGIL_CORS_ORIGINS (comma-separated).
# Default keeps the existing dev hosts; production deployments must override.
_DEFAULT_CORS_ORIGINS = [
    "http://localhost:6988",
    "http://127.0.0.1:6988",
    "http://localhost:3000",
    "http://localhost:5173",
]
_cors_origins_raw = get_settings().vigil_cors_origins
if _cors_origins_raw:
    _cors_origins = [o.strip() for o in _cors_origins_raw.split(",") if o.strip()]
else:
    _cors_origins = _DEFAULT_CORS_ORIGINS

app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=[
        "Authorization",
        "Content-Type",
        "X-CSRF-Token",
        "X-MFA-Required",
        "X-Requested-With",
    ],
    expose_headers=["X-MFA-Required"],
)

# CSRF middleware. No-op by default (VIGIL_CSRF_ENABLED=false); PR 4 flips
# it on once the frontend uses HttpOnly cookies and echoes X-CSRF-Token.
# Registered between CORS and SecurityHeaders so:
#   - SecurityHeaders (outermost) applies to any 403 CSRF rejection.
#   - CORS (innermost of these three) still short-circuits OPTIONS preflight.
app.add_middleware(CSRFMiddleware)

# Security headers added AFTER CORS so it is the outermost middleware on the
# response path. That way HSTS/CSP/X-Frame-Options apply to CORS preflight
# responses too (CORSMiddleware short-circuits OPTIONS without calling inner
# middleware, so anything added before CORS would be skipped on preflight).
app.add_middleware(SecurityHeadersMiddleware)

if PROMETHEUS_AVAILABLE:
    app.add_middleware(PrometheusMiddleware)

# Include API routers

# Authentication routes: login/refresh/password-reset are public; the
# inner /me, /change-password, /mfa routes already use get_current_active_user
# inline. Leaving the router itself unwrapped preserves that mix.
app.include_router(auth_router, prefix=f"{_CONTEXT_PATH}/api/auth", tags=["authentication"])
app.include_router(
    users_router, prefix=f"{_CONTEXT_PATH}/api/users", tags=["users"], dependencies=AUTH_DEPENDENCY
)

# JIRA export
app.include_router(
    jira_export_router,
    prefix=f"{_CONTEXT_PATH}/api",
    tags=["jira-export"],
    dependencies=AUTH_DEPENDENCY,
)

# Analytics
app.include_router(
    analytics_router, prefix=f"{_CONTEXT_PATH}/api", tags=["analytics"], dependencies=AUTH_DEPENDENCY
)
# Budgets — same prefix as analytics so /api/analytics/budget* lives next
# to /api/analytics/cost. The router itself owns the /analytics path
# segment per its endpoint definitions.
app.include_router(
    budgets_router, prefix=f"{_CONTEXT_PATH}/api", tags=["budgets"], dependencies=AUTH_DEPENDENCY
)

# Core API endpoints
app.include_router(
    findings_router,
    prefix=f"{_CONTEXT_PATH}/api/findings",
    tags=["findings"],
    dependencies=AUTH_DEPENDENCY,
)
app.include_router(
    cases_router, prefix=f"{_CONTEXT_PATH}/api/cases", tags=["cases"], dependencies=AUTH_DEPENDENCY
)
app.include_router(
    mcp_router, prefix=f"{_CONTEXT_PATH}/api/mcp", tags=["mcp"], dependencies=AUTH_DEPENDENCY
)

# Claude routes expose AI and agent execution capabilities and must require
# an authenticated user session. Keep rate limiting in addition to auth.
app.include_router(
    claude_router,
    prefix=f"{_CONTEXT_PATH}/api/claude",
    tags=["claude"],
    dependencies=[*AUTH_DEPENDENCY, Depends(rate_limit_dependency)],
)
app.include_router(
    reasoning_router,
    prefix=f"{_CONTEXT_PATH}/api/reasoning",
    tags=["reasoning"],
    dependencies=AUTH_DEPENDENCY,
)
app.include_router(
    conversations_router,
    prefix=f"{_CONTEXT_PATH}/api/conversations",
    tags=["conversations"],
    dependencies=AUTH_DEPENDENCY,
)
app.include_router(
    config_router, prefix=f"{_CONTEXT_PATH}/api/config", tags=["config"], dependencies=AUTH_DEPENDENCY
)
app.include_router(
    llm_providers_router,
    prefix=f"{_CONTEXT_PATH}/api/llm/providers",
    tags=["llm-providers"],
    dependencies=AUTH_DEPENDENCY,
)
app.include_router(
    ai_config_router, prefix=f"{_CONTEXT_PATH}/api/ai", tags=["ai-config"], dependencies=AUTH_DEPENDENCY
)
app.include_router(
    attack_router, prefix=f"{_CONTEXT_PATH}/api/attack", tags=["attack"], dependencies=AUTH_DEPENDENCY
)
app.include_router(
    custom_agents_router,
    prefix=f"{_CONTEXT_PATH}/api",
    tags=["custom-agents"],
    dependencies=AUTH_DEPENDENCY,
)
app.include_router(
    agents_router, prefix=f"{_CONTEXT_PATH}/api/agents", tags=["agents"], dependencies=AUTH_DEPENDENCY
)
app.include_router(
    compatibility_router,
    prefix=f"{_CONTEXT_PATH}/api/integrations",
    tags=["integrations"],
    dependencies=AUTH_DEPENDENCY,
)
app.include_router(
    custom_integrations_router,
    prefix=f"{_CONTEXT_PATH}/api/custom-integrations",
    tags=["custom-integrations"],
    dependencies=AUTH_DEPENDENCY,
)
app.include_router(
    skills_router,
    prefix=f"{_CONTEXT_PATH}/api/skills",
    tags=["skills"],
    dependencies=AUTH_DEPENDENCY,
)
app.include_router(
    ingestion_router,
    prefix=f"{_CONTEXT_PATH}/api/ingest",
    tags=["ingestion"],
    dependencies=AUTH_DEPENDENCY,
)
# VStrike /findings is public-but-bearer-authenticated inside the router.
# All VStrike management/UI/proxy routes require an authenticated user session.
app.include_router(vstrike_router, prefix=f"{_CONTEXT_PATH}/api/integrations/vstrike", tags=["vstrike"])
# Page-extension host — mints connector session tokens.
app.include_router(
    extensions_router,
    prefix=f"{_CONTEXT_PATH}/api/integrations",
    tags=["extensions"],
)
app.include_router(
    storage_status_router,
    prefix=f"{_CONTEXT_PATH}/api/storage",
    tags=["storage"],
    dependencies=AUTH_DEPENDENCY,
)
app.include_router(
    ai_decisions_router,
    prefix=f"{_CONTEXT_PATH}/api/ai",
    tags=["ai-decisions"],
    dependencies=AUTH_DEPENDENCY,
)
app.include_router(
    timeline_router,
    prefix=f"{_CONTEXT_PATH}/api/timeline",
    tags=["timeline"],
    dependencies=AUTH_DEPENDENCY,
)
app.include_router(
    graph_router, prefix=f"{_CONTEXT_PATH}/api/graph", tags=["graph"], dependencies=AUTH_DEPENDENCY
)
app.include_router(
    logs_router, prefix=f"{_CONTEXT_PATH}/api/logs", tags=["logs"], dependencies=AUTH_DEPENDENCY
)
app.include_router(
    local_services_router,
    prefix=f"{_CONTEXT_PATH}/api/services",
    tags=["local-services"],
    dependencies=AUTH_DEPENDENCY,
)
app.include_router(
    detection_rules_router,
    prefix=f"{_CONTEXT_PATH}/api/detection-rules",
    tags=["detection-rules"],
    dependencies=AUTH_DEPENDENCY,
)

# Workflows engine
app.include_router(
    workflows_router, prefix=f"{_CONTEXT_PATH}/api", tags=["workflows"], dependencies=AUTH_DEPENDENCY
)
app.include_router(
    approvals_router, prefix=f"{_CONTEXT_PATH}/api", tags=["approvals"], dependencies=AUTH_DEPENDENCY
)

# Autonomous orchestrator
app.include_router(
    orchestrator_router,
    prefix=f"{_CONTEXT_PATH}/api/orchestrator",
    tags=["orchestrator"],
    dependencies=AUTH_DEPENDENCY,
)
# Federated monitoring (per-source SIEM/EDR pull driven by federation_sources)
app.include_router(
    federation_router,
    prefix=f"{_CONTEXT_PATH}/api/federation",
    tags=["federation"],
    dependencies=AUTH_DEPENDENCY,
)
app.include_router(kafka_router, prefix=_CONTEXT_PATH, dependencies=AUTH_DEPENDENCY)

# Enhanced case management routers
app.include_router(
    case_templates_router,
    prefix=f"{_CONTEXT_PATH}/api/cases/templates",
    tags=["case-templates"],
    dependencies=AUTH_DEPENDENCY,
)
app.include_router(
    case_metrics_router,
    prefix=f"{_CONTEXT_PATH}/api/cases/metrics",
    tags=["case-metrics"],
    dependencies=AUTH_DEPENDENCY,
)
app.include_router(
    case_search_router,
    prefix=f"{_CONTEXT_PATH}/api/cases/search",
    tags=["case-search"],
    dependencies=AUTH_DEPENDENCY,
)
# Webhook management routes require authenticated user sessions.
# Inbound third-party webhook receivers should live in dedicated routers
# with endpoint-specific HMAC/API-key validation.
app.include_router(
    webhooks_router,
    prefix=f"{_CONTEXT_PATH}/api/webhooks",
    tags=["webhooks"],
    dependencies=AUTH_DEPENDENCY,
)
# Darktrace inbound webhook receiver — only mount when explicitly enabled.
# env.example and docs/integrations/DARKTRACE.md document DARKTRACE_ENABLED
# as the on/off toggle; leaving it unset must leave the receiver off.
if get_settings().darktrace_enabled:
    app.include_router(
        darktrace_webhook_router,
        prefix=f"{_CONTEXT_PATH}/api/webhooks/darktrace",
        tags=["darktrace"],
    )

# Cloudflare Cloudy webhook — only mount when explicitly enabled. Hard-off by
# default since the upstream API contract is not yet stable; flip
# CLOUDY_INGESTION_ENABLED=true (or system_config cloudflare.cloudy.enabled)
# once the partnership confirms the wire format. The endpoint itself also
# checks the flag at request time, so even a misconfigured mount fails closed.
if cloudy_ingestion_enabled():
    app.include_router(
        cloudflare_webhooks_router,
        prefix=f"{_CONTEXT_PATH}/api/webhooks/cloudflare",
        tags=["cloudflare"],
    )
app.include_router(
    sla_policies_router,
    prefix=f"{_CONTEXT_PATH}/api/sla-policies",
    tags=["sla-policies"],
    dependencies=AUTH_DEPENDENCY,
)


def _mcp_auto_connect_enabled() -> bool:
    # Off by default in DEV_MODE so optional MCP processes cannot block a local
    # backend startup; an explicit setting wins either way.
    settings = get_settings()
    if settings.mcp_auto_connect_on_startup is not None:
        return settings.mcp_auto_connect_on_startup
    return not settings.dev_mode


# Connects external startup integrations (skipped under TESTING).
async def _connect_external_services(mcp_client):
    import asyncio

    try:
        from core.llm.bifrost.admin import sync_all_provider_keys

        sync_all_provider_keys()
    except Exception as e:
        logger.warning(f"Bifrost provider sync skipped: {e}")

    try:
        from core.llm.bifrost.admin import sync_all_provider_models

        refresh_interval_s = get_settings().model_catalog_refresh_interval_s

        async def _model_catalog_refresher():
            while True:
                try:
                    await sync_all_provider_models()
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "Model catalog refresh iteration failed: %s",
                        exc,
                    )
                if refresh_interval_s <= 0:
                    break
                await asyncio.sleep(refresh_interval_s)

        asyncio.create_task(_model_catalog_refresher())
    except Exception as e:
        logger.warning(f"Model catalog refresher skipped: {e}")

    logger.info("Initializing LLM Gateway (ARQ / Redis)...")
    try:
        from core.llm.gateway.gateway import get_llm_gateway

        await get_llm_gateway()
        logger.info("✓ LLM Gateway connected to Redis")
    except Exception as e:
        logger.warning(f"⚠ LLM Gateway not available: {e}")
        logger.warning(
            "  LLM calls will fail until Redis is running and ARQ worker is started"
        )

    # MCP tools connect lazily when an agent actually needs them. Starting
    # every configured stdio server during local development makes the entire
    # API unavailable if an optional integration has a stale executable or
    # missing runtime. Preserve eager connection outside DEV_MODE unless an
    # operator explicitly overrides it.
    if not _mcp_auto_connect_enabled():
        logger.info(
            "MCP auto-connect disabled; optional MCP servers will connect on demand"
        )
        return

    logger.info("Initializing MCP client with persistent connections...")
    try:
        if mcp_client:
            mcp_service = mcp_client.mcp_service
            servers = mcp_service.list_servers()

            connected_count = 0
            for server_name in servers:
                try:
                    success = await mcp_client.connect_to_server(
                        server_name, persistent=True
                    )
                    if success:
                        connected_count += 1
                        logger.info(
                            f"✓ Persistent connection established: {server_name}"
                        )
                    else:
                        missing = mcp_client.get_missing_credentials(server_name)
                        if missing:
                            logger.info(
                                "MCP server %s dormant — awaiting env vars: %s",
                                server_name,
                                ", ".join(missing),
                            )
                        else:
                            logger.warning(
                                f"Failed to connect to MCP server: {server_name}"
                            )
                except Exception as e:
                    logger.error(f"Error connecting to {server_name}: {e}")

            logger.info(
                f"MCP initialization complete: {connected_count}/{len(servers)} persistent connections"
            )

            tools = await mcp_client.list_tools()
            total_tools = sum(len(t) for t in tools.values())
            logger.info(f"Loaded {total_tools} MCP tools from {len(tools)} servers")

            try:
                cache_dir = Path(__file__).parent.parent / "data"
                cache_dir.mkdir(parents=True, exist_ok=True)
                cache_file = cache_dir / "mcp_tools_cache.json"

                cache_data = {}
                for server_name, server_tools in tools.items():
                    cache_data[server_name] = []
                    for tool in server_tools:
                        input_schema = tool.get("inputSchema", {})
                        if hasattr(input_schema, "model_dump"):
                            input_schema = input_schema.model_dump()
                        elif not isinstance(input_schema, dict):
                            input_schema = dict(input_schema) if input_schema else {}
                        cache_data[server_name].append(
                            {
                                "name": tool.get("name"),
                                "description": tool.get("description", ""),
                                "inputSchema": input_schema,
                            }
                        )

                with open(cache_file, "w") as f:
                    json.dump(cache_data, f, indent=2)

                logger.info(f"✓ Saved MCP tools cache to {cache_file}")
            except Exception as e:
                logger.warning(f"⚠ Could not save MCP tools cache: {e}")

            status = mcp_client.get_connection_status()
            logger.info(
                f"Persistent connections: {sum(1 for connected in status.values() if connected)}/{len(status)}"
            )
        else:
            logger.warning("MCP client not available - MCP SDK may not be installed")
    except Exception as e:
        logger.error(f"Error during MCP initialization: {e}")


# Constructs the process-scoped services. This is the only place they are built for
# the API; handlers receive them through the Depends providers in backend/deps.py.
def _build_services(app: FastAPI):
    from core.config import is_demo_mode
    from services.agent_ai_generator import AgentAIGenerator
    from services.approval_service import ApprovalService
    from services.custom_workflow_service import CustomWorkflowService
    from services.demo_data_service import DemoDataService
    from services.detection_rules_service import DetectionRulesService
    from services.integration_bridge_service import IntegrationBridgeService
    from services.integration_compatibility_service import (
        IntegrationCompatibilityService,
    )
    from services.mcp_client import build_mcp_client, set_process_mcp_client
    from services.mcp_registry import MCPRegistry
    from services.workflow_ai_generator import WorkflowAIGenerator
    from services.workflow_run_service import WorkflowRunService
    from services.workflows_service import WorkflowsService

    app.state.mcp_client = build_mcp_client()
    set_process_mcp_client(app.state.mcp_client)

    app.state.approvals = ApprovalService()
    app.state.custom_workflows = CustomWorkflowService()
    app.state.detection_rules = DetectionRulesService()
    app.state.integration_bridge = IntegrationBridgeService()
    app.state.integration_compat = IntegrationCompatibilityService()
    app.state.mcp_registry = MCPRegistry()
    app.state.workflow_runs = WorkflowRunService()

    app.state.workflows = WorkflowsService(
        approvals=app.state.approvals,
        custom_workflows=app.state.custom_workflows,
        mcp_registry=app.state.mcp_registry,
        workflow_runs=app.state.workflow_runs,
    )
    app.state.workflow_ai = WorkflowAIGenerator(
        workflows=app.state.workflows, mcp_registry=app.state.mcp_registry
    )
    app.state.agent_ai = AgentAIGenerator(mcp_registry=app.state.mcp_registry)

    # Demo data is only generated when demo mode is on; generating it otherwise
    # burns startup time building 25 findings nothing will read.
    app.state.demo_data = DemoDataService() if is_demo_mode() else None


# Builds the process-scoped services onto app.state, then initializes the database,
# MCP tools and integration compatibility. Handlers reach these via backend/deps.py.
async def _startup(app: FastAPI):
    logger.info("=" * 60)
    logger.info("Starting Vigil SOC Backend")
    logger.info("=" * 60)

    _build_services(app)

    _testing = get_settings().testing

    # Keep the sync-endpoint threadpool in lockstep with the DB connection pool.
    # Starlette runs `def` endpoints (and sync deps) on anyio's default thread
    # limiter, which defaults to 40; our pool ceiling is pool_size + max_overflow
    # (30 by default). Without this, a burst of 31+ concurrent DB-bound requests
    # would occupy threads that then block on the pool up to DB_POOL_TIMEOUT.
    try:
        import anyio.to_thread
        from database.connection import get_db_manager

        _pool_cfg = get_db_manager().config
        _db_ceiling = _pool_cfg.pool_size + _pool_cfg.max_overflow
        anyio.to_thread.current_default_thread_limiter().total_tokens = _db_ceiling
        logger.info(
            "Threadpool capacity set to %d to match DB pool "
            "(pool_size=%d + max_overflow=%d)",
            _db_ceiling,
            _pool_cfg.pool_size,
            _pool_cfg.max_overflow,
        )
    except Exception as e:  # pragma: no cover - best-effort tuning
        logger.warning("Could not align threadpool with DB pool: %s", e)

    # Initialize Sentry error tracking (was never called before — bug fix)
    try:
        from backend.monitoring import init_sentry

        init_sentry()
    except Exception as e:
        logger.warning("Sentry initialization failed (non-fatal): %s", e)

    # Probe the secrets manager singleton at a known time, after all
    # third-party imports have settled. The singleton picks its write
    # backend on first init and never re-evaluates, so if `cryptography`
    # was unavailable at the moment of an earlier import-time call the
    # whole process would fall through to the dotenv backend silently.
    # Logging here gives us an explicit signal whenever that happens.
    try:
        from backend.secrets_manager import get_secrets_manager

        _mgr = get_secrets_manager()
        _status = _mgr.get_backend_status()
        if _status["write_backend"] != _status["expected_write_backend"]:
            logger.error(
                "SecretsManager init: write_backend=%s but expected=%s "
                "(cryptography_available=%s, master_key_present=%s). "
                "POST /api/config/secrets/reinit to retry without restart.",
                _status["write_backend"],
                _status["expected_write_backend"],
                _status["cryptography_available"],
                _status["encrypted"]["master_key_present"],
            )
        else:
            logger.info(
                "SecretsManager ready: write_backend=%s, " "cryptography_available=%s",
                _status["write_backend"],
                _status["cryptography_available"],
            )
    except Exception as e:  # pragma: no cover - probe is best-effort
        logger.warning("SecretsManager startup probe failed: %s", e)

    # Load secrets into environment for MCP servers
    try:
        from backend.secrets_manager import get_secret

        # Load PostgreSQL connection string for database backend
        postgres_conn = get_secret("POSTGRESQL_CONNECTION_STRING")
        if postgres_conn:
            os.environ["POSTGRESQL_CONNECTION_STRING"] = postgres_conn  # noqa: ENV001
            logger.debug("Loaded PostgreSQL connection string from secrets")
        else:
            # Set default connection string if not configured
            default_conn = "postgresql://deeptempo:deeptempo_secure_password_change_me@localhost:5432/deeptempo_soc"
            os.environ["POSTGRESQL_CONNECTION_STRING"] = default_conn  # noqa: ENV001
            logger.debug("Using default PostgreSQL connection string")

        # Rehydrate integration credentials into os.environ so MCP servers gated
        # on ${<ID>_<FIELD>} survive a restart — set_secret only writes os.environ
        # in the saving process, so without this they'd go dormant.
        from services.integration_secrets import INTEGRATION_SECRET_FIELDS

        rehydrated = 0
        for field_map in INTEGRATION_SECRET_FIELDS.values():
            for env_key in field_map.values():
                value = get_secret(env_key)
                if value:
                    os.environ[env_key] = value  # noqa: ENV001 - MCP child env
                    rehydrated += 1
        logger.debug("Rehydrated %d integration secret(s) into env", rehydrated)

    except Exception as e:
        logger.warning(f"Error loading secrets for MCP servers: {e}")

    # A configured connector with no allowlisted origin will be blocked by the
    # default CSP — warn rather than fail silently. Best-effort.
    try:
        from services.extension_trust import connector_allowlist_origins

        if not connector_allowlist_origins():
            cfg = app.state.integration_bridge.load_integration_config()
            connectors = [
                iid
                for iid, c in (cfg.get("integrations") or {}).items()
                if (c or {}).get("connectorUrl")
            ]
            if connectors:
                logger.warning(
                    "Page-extension connector(s) %s are configured but "
                    "EXTENSION_CONNECTOR_ALLOWLIST is empty; their UI bundle "
                    "import and BFF calls will be blocked by the "
                    "Content-Security-Policy. Add each connector's origin to "
                    "EXTENSION_CONNECTOR_ALLOWLIST.",
                    connectors,
                )
    except Exception as e:  # pragma: no cover - defensive
        logger.debug("Connector allowlist check skipped: %s", e)

    # Initialize data storage backend
    logger.info("Initializing data storage...")
    try:
        from services.database_data_service import DatabaseDataService
        from core.config import is_demo_mode

        # Defense-in-depth: ensure the SQLAlchemy-managed schema exists before
        # any endpoint tries to query it. start.sh runs scripts/init_schema.py
        # first, but this covers environments that launch uvicorn directly
        # (e.g. Docker, systemd, CI). When DATA_BACKEND=database, a failure
        # here is fatal — we do NOT silently fall back to JSON because that
        # leaves the DB in an inconsistent state (some endpoints use
        # get_db_session() directly, see backend/api/case_metrics.py).
        data_backend_env = get_settings().data_backend.lower()
        if not is_demo_mode() and data_backend_env == "database":
            try:
                from database.connection import init_database

                init_database(echo=False, create_tables=True)
                logger.info("✓ Database schema ensured (create_all)")
            except Exception as schema_err:
                logger.error(
                    "Fatal: could not initialize database schema: %s",
                    schema_err,
                )
                raise

        # Check for demo mode first
        if is_demo_mode():
            logger.info("=" * 40)
            logger.info("  DEMO MODE ENABLED")
            logger.info("  Using generated sample data")
            logger.info("  Set DEMO_MODE=false to disable")
            logger.info("=" * 40)
            test_service = DatabaseDataService()
            backend_info = test_service.get_backend_info()
            logger.info(f"  Backend: {backend_info['backend']}")
        else:
            # Check configuration preference
            data_backend = get_settings().data_backend.lower()
            use_database = data_backend == "database"

            if use_database:
                logger.info("Attempting to connect to PostgreSQL database...")
                try:
                    test_service = DatabaseDataService()

                    if test_service.is_using_database():
                        logger.info("✓ PostgreSQL database connected and ready")
                        backend_info = test_service.get_backend_info()
                        logger.info(f"  Backend: {backend_info['backend']}")
                    else:
                        logger.warning("⚠ PostgreSQL not available")
                        logger.warning("  Using JSON file storage as fallback")
                        logger.warning("  To enable PostgreSQL:")
                        logger.warning(
                            "    1. Start database: "
                            "cd docker && docker compose up -d postgres"
                        )
                        logger.warning("    2. Restart application: ./start.sh")

                except Exception as e:
                    logger.warning(f"⚠ Could not connect to PostgreSQL: {e}")
                    logger.warning("  Using JSON file storage as fallback")
            else:
                logger.info("Using JSON file storage (DATA_BACKEND=json)")

    except ImportError as e:
        logger.warning(f"Database modules not available: {e}")
        logger.warning("Using JSON file storage")
    except Exception as e:
        logger.error(f"Error during storage initialization: {e}")
        logger.warning("Falling back to JSON file storage")

    # Check integration compatibility
    logger.info("Checking integration compatibility...")
    try:
        compat_service = app.state.integration_compat
        system_info = compat_service.get_system_info()
        logger.info(
            f"System: Python {system_info['python_version']} on {system_info['platform']}"
        )

        # Log compatibility issues
        statuses = compat_service.get_all_statuses()
        incompatible = [
            k for k, v in statuses.items() if v.get("status") == "incompatible"
        ]
        not_installed = [
            k for k, v in statuses.items() if v.get("status") == "not_installed"
        ]

        if incompatible:
            logger.warning(f"Incompatible integrations: {', '.join(incompatible)}")
        if not_installed:
            logger.info(f"Not installed integrations: {', '.join(not_installed)}")

        installed_count = sum(1 for v in statuses.values() if v.get("installed"))
        logger.info(f"Integration status: {installed_count}/{len(statuses)} installed")
    except Exception as e:
        logger.error(f"Error checking compatibility: {e}")

    if _testing:
        logger.info(
            "TESTING=true — skipping external startup connections "
            "(Bifrost sync, model catalog, LLM gateway, MCP)"
        )
    else:
        await _connect_external_services(app.state.mcp_client)

    # Load custom agents from DB into the AgentManager so built-in + custom
    # agents are visible in one merged list. Lookup misses for "custom-*" IDs
    # also trigger a refresh at request time, so this is a convenience preload.
    try:
        from backend.api.agents import agent_manager

        loaded = agent_manager.refresh_custom_agents()
        logger.info(f"Loaded {loaded} custom agent(s) from database")
    except Exception as e:
        logger.warning(f"Could not preload custom agents: {e}")


# Cleans up the LLM gateway and MCP connections.
async def _shutdown(app: FastAPI):
    logger.info("Shutting down LLM Gateway...")
    try:
        from core.llm.gateway.gateway import close_llm_gateway

        await close_llm_gateway()
        logger.info("LLM Gateway closed")
    except Exception as e:
        logger.error(f"Error closing LLM Gateway: {e}")

    logger.info("Shutting down MCP connections...")
    try:
        from services.mcp_client import set_process_mcp_client

        mcp_client = getattr(app.state, "mcp_client", None)
        if mcp_client:
            # Close all MCP sessions — stdio child processes are owned by
            # the MCP SDK's stdio_client contexts, which shut down as the
            # persistent sessions close. No separate Popen pool to tear
            # down since the legacy start_server path was removed (#125).
            await mcp_client.close_all()
            logger.info("All MCP connections closed")
        set_process_mcp_client(None)
    except Exception as e:
        logger.error(f"Error during shutdown cleanup: {e}")

    # Flush and shut down OTEL providers
    try:
        from core.telemetry import shutdown_telemetry

        shutdown_telemetry()
    except Exception as e:
        logger.warning("Telemetry shutdown error (non-fatal): %s", e)


# Prometheus metrics endpoint
@app.get("/metrics", include_in_schema=False)
async def metrics():
    return get_metrics_response()


# Health check endpoint
@app.get(f"{_CONTEXT_PATH}/api/health")
async def health_check():
    try:
        from services.database_data_service import DatabaseDataService
        from core.config import is_demo_mode

        service = DatabaseDataService()
        backend_info = service.get_backend_info()

        return {
            "status": "healthy",
            "version": __version__,
            "demo_mode": is_demo_mode(),
            "storage": {
                "backend": backend_info["backend"],
                "database_available": backend_info.get("database_available", False),
                "demo_mode": backend_info.get("demo_mode", False),
            },
        }
    except Exception as e:
        logger.error(f"Health check error: {e}")
        return {
            "status": "healthy",
            "version": __version__,
            "demo_mode": False,
            "storage": {"backend": "unknown", "error": str(e)},
        }


# Serve React static files in production
frontend_build_dir = Path(__file__).parent.parent / "clients" / "web" / "build"
static_dir = frontend_build_dir / "static"

# Only mount static files if the build directory exists
# This prevents errors during development when frontend hasn't been built
if frontend_build_dir.exists() and static_dir.exists():
    try:
        app.mount(f"{_CONTEXT_PATH}/static", StaticFiles(directory=static_dir), name="static")
        logger.info(f"Serving static files from: {static_dir}")
    except Exception as e:
        logger.warning(f"Failed to mount static files: {e}")
else:
    logger.info("Frontend build directory not found - static file serving disabled")
    logger.info(f"  Expected: {frontend_build_dir}")
    logger.info(
        "  Run 'npm run build' in the frontend directory to enable production mode"
    )

# Vite emits hashed JS/CSS bundles under build/assets and references them as
# /assets/* from index.html. Mount that directory explicitly; otherwise the
# catch-all below returns index.html (text/html) for every bundle request and
# the browser refuses to execute the module ("disallowed MIME type").
assets_dir = frontend_build_dir / "assets"
if frontend_build_dir.exists() and assets_dir.exists():
    try:
        app.mount(f"{_CONTEXT_PATH}/assets", StaticFiles(directory=assets_dir), name="assets")
        logger.info(f"Serving frontend assets from: {assets_dir}")
    except Exception as e:
        logger.warning(f"Failed to mount frontend assets: {e}")

if frontend_build_dir.exists() and (frontend_build_dir / "index.html").exists():
    from fastapi.responses import HTMLResponse

    # index.html is served with the active context path injected as a
    # <meta name="vigil-base-path"> tag so the SPA (see frontend
    # src/config/basePath.ts) can prefix its router basename and API calls at
    # runtime — no rebuild needed per deployment path. A meta tag (not an inline
    # <script>) is used because the CSP is script-src 'self', which blocks inline
    # scripts. Cached after first read.
    _index_html_cache: "str | None" = None

    def _get_index_html() -> str:
        global _index_html_cache
        if _index_html_cache is None:
            raw = (frontend_build_dir / "index.html").read_text()
            tags = [f'<meta name="vigil-base-path" content="{_CONTEXT_PATH}">']
            # Expose the connector allowlist to the SPA (same source as the CSP +
            # SSRF guard). A <meta>, not an inline <script>, because CSP is
            # script-src 'self'.
            try:
                from services.extension_trust import connector_allowlist_origins

                origins = connector_allowlist_origins()
                if origins:
                    allow = ",".join(origins)
                    tags.append(
                        f'<meta name="vigil-extension-allowlist" content="{allow}">'
                    )
            except Exception as e:  # pragma: no cover - defensive
                logger.debug("extension allowlist meta injection skipped: %s", e)
            injected = "\n    ".join(tags)
            _index_html_cache = raw.replace("<head>", f"<head>\n    {injected}", 1)
        return _index_html_cache

    # When served under a context path, redirect the bare path (no trailing
    # slash) to the slash form so relative asset URLs resolve correctly.
    if _CONTEXT_PATH:
        from fastapi.responses import RedirectResponse

        @app.get(_CONTEXT_PATH, include_in_schema=False)
        async def redirect_to_trailing_slash():
            return RedirectResponse(url=f"{_CONTEXT_PATH}/", status_code=301)

    @app.get(f"{_CONTEXT_PATH}/{{full_path:path}}")
    async def serve_react_app(full_path: str):
        # Don't interfere with API routes
        if full_path.startswith("api/"):
            return {"error": "Not found"}, 404
        return HTMLResponse(_get_index_html())


if __name__ == "__main__":
    import uvicorn

    logger.info("Starting Vigil SOC API server...")
    uvicorn.run(
        "backend.main:app", host="0.0.0.0", port=6987, reload=True, log_level="info"
    )
