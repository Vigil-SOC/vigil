"""
FastAPI Backend for Vigil SOC Web Application

Main application entry point for the REST API server.
"""

import json
import logging
import os
import sys
from contextlib import asynccontextmanager
from pathlib import Path

# Add the repo root to sys.path so `core.*` and `services.*` resolve whether
# the app is launched as `services.api.main:app` or imported directly. This
# file lives at services/api/main.py, so the repo root is three parents up.
_repo_root = Path(__file__).resolve().parents[2]
project_root = str(_repo_root)
if project_root not in sys.path:
    sys.path.insert(0, project_root)

from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded

from core.config import get_settings
from core.platform.monitoring import (
    PROMETHEUS_AVAILABLE,
    get_metrics_response,
    init_sentry,
)
from core.version import __version__
from services.api.discovery import mount_routers
from services.api.middleware.auth import get_current_active_user
from services.api.middleware.csrf import CSRFMiddleware
from services.api.middleware.rate_limit import limiter
from services.api.middleware.security_headers import SecurityHeadersMiddleware

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
        # live on an empty instance (see services/api/routers/auth.py bootstrap).
        "/api/auth/bootstrap",
        # Health check — used by load balancers and Docker.
        "/api/health",
        # VStrike inbound receiver uses its own bearer API-key dependency.
        "/api/integrations/vstrike/findings",
    }
)

if PROMETHEUS_AVAILABLE:
    from core.platform.monitoring import PrometheusMiddleware

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

# Mount every discovered router — colocated in core/<domain>/ or parked in
# services/api/routers/ (issues #478, #488). Each module declares its
# own prefix, tags, auth posture and optional feature gate in ROUTER_META, so
# adding a router needs no edit to this file. See core/routing.py.
mount_routers(
    app,
    context_path=_CONTEXT_PATH,
    auth_dependency=AUTH_DEPENDENCY,
)


def _mcp_auto_connect_enabled() -> bool:
    # Off by default in DEV_MODE so optional MCP processes cannot block a local
    # backend startup; an explicit setting wins either way.
    settings = get_settings()
    if settings.mcp_auto_connect_on_startup is not None:
        return settings.mcp_auto_connect_on_startup
    return not settings.dev_mode


async def _connect_external_services(mcp_client):
    """Connect external startup integrations (skipped under TESTING)."""
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
                cache_dir = _repo_root / "data"
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
# the API; handlers receive them through the Depends providers in core/deps.py.
def _build_services(app: FastAPI):
    from core.agents.agent_ai_generator import AgentAIGenerator
    from core.config import is_demo_mode
    from core.detections.detection_rules_service import DetectionRulesService
    from core.integrations.integration_bridge_service import IntegrationBridgeService
    from core.integrations.integration_compatibility_service import (
        IntegrationCompatibilityService,
    )
    from core.integrations.mcp.client import build_mcp_client, set_process_mcp_client
    from core.integrations.mcp.registry import MCPRegistry
    from core.platform.demo_data_service import DemoDataService
    from core.response.approval_service import ApprovalService
    from core.workflows.custom_workflow_service import CustomWorkflowService
    from core.workflows.workflow_ai_generator import WorkflowAIGenerator
    from core.workflows.workflow_run_service import WorkflowRunService
    from core.workflows.workflows_service import WorkflowsService

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
    # burns startup time building findings nothing will read.
    app.state.demo_data = DemoDataService() if is_demo_mode() else None


async def _startup(app: FastAPI):
    """Initialize database, MCP tools and check integration compatibility on startup."""
    logger.info("=" * 60)
    logger.info("Starting Vigil SOC Backend")
    logger.info("=" * 60)

    _build_services(app)

    _testing = get_settings().testing

    # Initialize Sentry error tracking (was never called before — bug fix)
    try:
        from core.platform.monitoring import init_sentry

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
        from core.secrets_manager import get_secrets_manager

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
        from core.secrets_manager import get_secret

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
        from core.integrations.integration_secrets import INTEGRATION_SECRET_FIELDS

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
        from core.integrations.extension.trust import connector_allowlist_origins

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
        from core.config import is_demo_mode
        from core.storage.database_data_service import DatabaseDataService

        # Defense-in-depth: ensure the SQLAlchemy-managed schema exists before
        # any endpoint tries to query it. start.sh runs scripts/init_schema.py
        # first, but this covers environments that launch uvicorn directly
        # (e.g. Docker, systemd, CI). When DATA_BACKEND=database, a failure
        # here is fatal — we do NOT silently fall back to JSON because that
        # leaves the DB in an inconsistent state (some endpoints use
        # get_db_session() directly, see core/cases/case_metrics_router.py).
        data_backend_env = get_settings().data_backend.lower()
        if not is_demo_mode() and data_backend_env == "database":
            try:
                from core.storage.connection import init_database

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
        from services.api.routers.agents import agent_manager

        loaded = agent_manager.refresh_custom_agents()
        logger.info(f"Loaded {loaded} custom agent(s) from database")
    except Exception as e:
        logger.warning(f"Could not preload custom agents: {e}")


async def _shutdown(app: FastAPI):
    """Clean up LLM gateway and MCP connections on shutdown."""
    logger.info("Shutting down LLM Gateway...")
    try:
        from core.llm.gateway.gateway import close_llm_gateway

        await close_llm_gateway()
        logger.info("LLM Gateway closed")
    except Exception as e:
        logger.error(f"Error closing LLM Gateway: {e}")

    logger.info("Shutting down MCP connections...")
    try:
        from core.integrations.mcp.client import set_process_mcp_client

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
    """Expose Prometheus metrics for scraping."""
    return get_metrics_response()


# Health check endpoint
@app.get(f"{_CONTEXT_PATH}/api/health")
async def health_check():
    """Health check endpoint with storage backend info."""
    try:
        from core.config import is_demo_mode
        from core.storage.database_data_service import DatabaseDataService

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
frontend_build_dir = _repo_root / "clients" / "web" / "build"
static_dir = frontend_build_dir / "static"

# Only mount static files if the build directory exists
# This prevents errors during development when frontend hasn't been built
if frontend_build_dir.exists() and static_dir.exists():
    try:
        app.mount(
            f"{_CONTEXT_PATH}/static", StaticFiles(directory=static_dir), name="static"
        )
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
        app.mount(
            f"{_CONTEXT_PATH}/assets", StaticFiles(directory=assets_dir), name="assets"
        )
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
                from core.integrations.extension.trust import (
                    connector_allowlist_origins,
                )

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
        """Serve React app for all non-API routes."""
        # Don't interfere with API routes
        if full_path.startswith("api/"):
            return {"error": "Not found"}, 404
        return HTMLResponse(_get_index_html())


if __name__ == "__main__":
    import uvicorn

    logger.info("Starting Vigil SOC API server...")
    uvicorn.run(
        "services.api.main:app",
        host="0.0.0.0",
        port=6987,
        reload=True,
        log_level="info",
    )
