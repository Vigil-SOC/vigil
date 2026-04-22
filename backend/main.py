"""
FastAPI Backend for Vigil SOC Web Application

Main application entry point for the REST API server.
"""

import json
import logging
import os
import sys
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
    reasoning_router,
    skills_router,
    llm_providers_router,
)
from api.local_services import router as local_services_router
from api.integrations_compatibility import router as compatibility_router
from api.ingestion import router as ingestion_router
from api.timeline import router as timeline_router
from api.graph import router as graph_router
from api.vstrike import router as vstrike_router
from api.custom_agents import router as custom_agents_router

# Enhanced case management routers
from api.case_templates import router as case_templates_router
from api.case_metrics import router as case_metrics_router
from api.case_search import router as case_search_router
from api.webhooks import router as webhooks_router
from api.sla_policies import router as sla_policies_router

# Darktrace inbound webhook receiver
from api.darktrace_webhook import router as darktrace_webhook_router

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

# Kafka ingestion router
from api.kafka import router as kafka_router

from core.rate_limit import rate_limit_dependency
from monitoring import init_sentry, PROMETHEUS_AVAILABLE, get_metrics_response
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

# Create FastAPI app
app = FastAPI(
    title="Vigil SOC API",
    description="REST API for Vigil SOC Application",
    version="1.0.0"
)

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
_cors_origins_raw = os.getenv("VIGIL_CORS_ORIGINS")
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

# Authentication (public endpoints)
app.include_router(auth_router, prefix="/api/auth", tags=["authentication"])
app.include_router(users_router, prefix="/api/users", tags=["users"])

# JIRA export
app.include_router(jira_export_router, prefix="/api", tags=["jira-export"])

# Analytics
app.include_router(analytics_router, prefix="/api", tags=["analytics"])

# Core API endpoints
app.include_router(findings_router, prefix="/api/findings", tags=["findings"])
app.include_router(cases_router, prefix="/api/cases", tags=["cases"])
app.include_router(mcp_router, prefix="/api/mcp", tags=["mcp"])
app.include_router(claude_router, prefix="/api/claude", tags=["claude"], dependencies=[Depends(rate_limit_dependency)])
app.include_router(reasoning_router, prefix="/api/reasoning", tags=["reasoning"])
app.include_router(config_router, prefix="/api/config", tags=["config"])
app.include_router(llm_providers_router, prefix="/api/llm/providers", tags=["llm-providers"])
app.include_router(attack_router, prefix="/api/attack", tags=["attack"])
app.include_router(custom_agents_router, prefix="/api", tags=["custom-agents"])
app.include_router(agents_router, prefix="/api/agents", tags=["agents"])
app.include_router(compatibility_router, prefix="/api/integrations", tags=["integrations"])
app.include_router(custom_integrations_router, prefix="/api/custom-integrations", tags=["custom-integrations"])
app.include_router(skills_router, prefix="/api/skills", tags=["skills"])
app.include_router(ingestion_router, prefix="/api/ingest", tags=["ingestion"])
app.include_router(vstrike_router, prefix="/api/integrations/vstrike", tags=["vstrike"])
app.include_router(storage_status_router, prefix="/api/storage", tags=["storage"])
app.include_router(ai_decisions_router, prefix="/api/ai", tags=["ai-decisions"])
app.include_router(timeline_router, prefix="/api/timeline", tags=["timeline"])
app.include_router(graph_router, prefix="/api/graph", tags=["graph"])
app.include_router(logs_router, prefix="/api/logs", tags=["logs"])
app.include_router(local_services_router, prefix="/api/services", tags=["local-services"])
app.include_router(detection_rules_router, prefix="/api/detection-rules", tags=["detection-rules"])

# Workflows engine
app.include_router(workflows_router, prefix="/api", tags=["workflows"])

# Autonomous orchestrator
app.include_router(orchestrator_router, prefix="/api/orchestrator", tags=["orchestrator"])
app.include_router(kafka_router)

# Enhanced case management routers
app.include_router(case_templates_router, prefix="/api/cases/templates", tags=["case-templates"])
app.include_router(case_metrics_router, prefix="/api/cases/metrics", tags=["case-metrics"])
app.include_router(case_search_router, prefix="/api/cases/search", tags=["case-search"])
app.include_router(webhooks_router, prefix="/api/webhooks", tags=["webhooks"])
# Darktrace inbound webhook receiver — only mount when explicitly enabled.
# env.example and docs/integrations/DARKTRACE.md document DARKTRACE_ENABLED
# as the on/off toggle; leaving it unset must leave the receiver off.
if os.environ.get("DARKTRACE_ENABLED", "false").lower() == "true":
    app.include_router(
        darktrace_webhook_router,
        prefix="/api/webhooks/darktrace",
        tags=["darktrace"],
    )
app.include_router(sla_policies_router, prefix="/api/sla-policies", tags=["sla-policies"])

@app.on_event("startup")
async def startup_event():
    """Initialize database, MCP tools and check integration compatibility on startup."""
    logger.info("=" * 60)
    logger.info("Starting Vigil SOC Backend")
    logger.info("=" * 60)

    # Initialize Sentry error tracking (was never called before — bug fix)
    try:
        from backend.monitoring import init_sentry
        init_sentry()
    except Exception as e:
        logger.warning("Sentry initialization failed (non-fatal): %s", e)

    # Load secrets into environment for MCP servers
    try:
        from backend.secrets_manager import get_secret
        import os
        
        # Load PostgreSQL connection string for database backend
        postgres_conn = get_secret("POSTGRESQL_CONNECTION_STRING")
        if postgres_conn:
            os.environ["POSTGRESQL_CONNECTION_STRING"] = postgres_conn
            logger.debug("Loaded PostgreSQL connection string from secrets")
        else:
            # Set default connection string if not configured
            default_conn = "postgresql://deeptempo:deeptempo_secure_password_change_me@localhost:5432/deeptempo_soc"
            os.environ["POSTGRESQL_CONNECTION_STRING"] = default_conn
            logger.debug("Using default PostgreSQL connection string")
            
        # Load GitHub token for MCP github server
        github_token = get_secret("GITHUB_TOKEN")
        if github_token:
            os.environ["GITHUB_TOKEN"] = github_token
            logger.debug("Loaded GitHub token from secrets")
            
    except Exception as e:
        logger.warning(f"Error loading secrets for MCP servers: {e}")
    
    # Initialize data storage backend
    logger.info("Initializing data storage...")
    try:
        from services.database_data_service import DatabaseDataService
        from core.config import is_demo_mode
        import os

        # Defense-in-depth: ensure the SQLAlchemy-managed schema exists before
        # any endpoint tries to query it. start_web.sh runs scripts/init_schema.py
        # first, but this covers environments that launch uvicorn directly
        # (e.g. Docker, systemd, CI). When DATA_BACKEND=database, a failure
        # here is fatal — we do NOT silently fall back to JSON because that
        # leaves the DB in an inconsistent state (some endpoints use
        # get_db_session() directly, see backend/api/case_metrics.py).
        data_backend_env = os.getenv('DATA_BACKEND', 'database').lower()
        if not is_demo_mode() and data_backend_env == 'database':
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
            data_backend = os.getenv('DATA_BACKEND', 'database').lower()
            use_database = data_backend == 'database'
            
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
                        logger.warning("    1. Start database: ./scripts/start_database.sh")
                        logger.warning("    2. Restart application: ./start_web.sh")
                    
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
        from services.integration_compatibility_service import get_compatibility_service
        
        compat_service = get_compatibility_service()
        system_info = compat_service.get_system_info()
        logger.info(f"System: Python {system_info['python_version']} on {system_info['platform']}")
        
        # Log compatibility issues
        statuses = compat_service.get_all_statuses()
        incompatible = [k for k, v in statuses.items() if v.get('status') == 'incompatible']
        not_installed = [k for k, v in statuses.items() if v.get('status') == 'not_installed']
        
        if incompatible:
            logger.warning(f"Incompatible integrations: {', '.join(incompatible)}")
        if not_installed:
            logger.info(f"Not installed integrations: {', '.join(not_installed)}")
        
        installed_count = sum(1 for v in statuses.values() if v.get('installed'))
        logger.info(f"Integration status: {installed_count}/{len(statuses)} installed")
    except Exception as e:
        logger.error(f"Error checking compatibility: {e}")
    
    # Initialize LLM Gateway (connects to Redis for ARQ job queue)
    logger.info("Initializing LLM Gateway (ARQ / Redis)...")
    try:
        from services.llm_gateway import get_llm_gateway
        await get_llm_gateway()
        logger.info("✓ LLM Gateway connected to Redis")
    except Exception as e:
        logger.warning(f"⚠ LLM Gateway not available: {e}")
        logger.warning("  LLM calls will fail until Redis is running and ARQ worker is started")
    
    logger.info("Initializing MCP client with persistent connections...")
    try:
        from services.mcp_client import get_mcp_client
        from services.mcp_service import MCPService
        import asyncio
        
        # Get MCP client and service
        mcp_client = get_mcp_client()
        
        if mcp_client:
            # Get list of all servers
            mcp_service = mcp_client.mcp_service
            servers = mcp_service.list_servers()
            
            # Connect to each server with persistent connections
            connected_count = 0
            for server_name in servers:
                try:
                    # persistent=True establishes a long-lived connection
                    success = await mcp_client.connect_to_server(server_name, persistent=True)
                    if success:
                        connected_count += 1
                        logger.info(f"✓ Persistent connection established: {server_name}")
                    else:
                        logger.warning(f"Failed to connect to MCP server: {server_name}")
                except Exception as e:
                    logger.error(f"Error connecting to {server_name}: {e}")
            
            logger.info(f"MCP initialization complete: {connected_count}/{len(servers)} persistent connections")
            
            # Log available tools
            tools = await mcp_client.list_tools()
            total_tools = sum(len(t) for t in tools.values())
            logger.info(f"Loaded {total_tools} MCP tools from {len(tools)} servers")

            # Persist tools to JSON cache file for access in async contexts
            try:
                cache_dir = Path(__file__).parent.parent / "data"
                cache_dir.mkdir(parents=True, exist_ok=True)
                cache_file = cache_dir / "mcp_tools_cache.json"

                cache_data = {}
                for server_name, server_tools in tools.items():
                    cache_data[server_name] = []
                    for tool in server_tools:
                        input_schema = tool.get("inputSchema", {})
                        if hasattr(input_schema, 'model_dump'):
                            input_schema = input_schema.model_dump()
                        elif not isinstance(input_schema, dict):
                            input_schema = dict(input_schema) if input_schema else {}
                        cache_data[server_name].append({
                            "name": tool.get("name"),
                            "description": tool.get("description", ""),
                            "inputSchema": input_schema
                        })

                with open(cache_file, 'w') as f:
                    json.dump(cache_data, f, indent=2)

                logger.info(f"✓ Saved MCP tools cache to {cache_file}")
            except Exception as e:
                logger.warning(f"⚠ Could not save MCP tools cache: {e}")

            # Log connection status
            status = mcp_client.get_connection_status()
            logger.info(f"Persistent connections: {sum(1 for connected in status.values() if connected)}/{len(status)}")
        else:
            logger.warning("MCP client not available - MCP SDK may not be installed")
    except Exception as e:
        logger.error(f"Error during MCP initialization: {e}")

    # Load custom agents from DB into the AgentManager so built-in + custom
    # agents are visible in one merged list. Lookup misses for "custom-*" IDs
    # also trigger a refresh at request time, so this is a convenience preload.
    try:
        from backend.api.agents import agent_manager
        loaded = agent_manager.refresh_custom_agents()
        logger.info(f"Loaded {loaded} custom agent(s) from database")
    except Exception as e:
        logger.warning(f"Could not preload custom agents: {e}")


@app.on_event("shutdown")
async def shutdown_event():
    """Clean up LLM gateway and MCP connections on shutdown."""
    logger.info("Shutting down LLM Gateway...")
    try:
        from services.llm_gateway import close_llm_gateway
        await close_llm_gateway()
        logger.info("LLM Gateway closed")
    except Exception as e:
        logger.error(f"Error closing LLM Gateway: {e}")
    
    logger.info("Shutting down MCP connections...")
    try:
        from services.mcp_client import get_mcp_client

        mcp_client = get_mcp_client()
        if mcp_client:
            # Close all MCP sessions
            await mcp_client.close_all()
            logger.info("All MCP connections closed")

            # Stop all MCP server processes managed by MCPService
            mcp_service = mcp_client.mcp_service
            if mcp_service:
                stop_results = mcp_service.stop_all()
                stopped_count = sum(1 for success in stop_results.values() if success)
                logger.info(f"Stopped {stopped_count} MCP server processes")
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
@app.get("/api/health")
async def health_check():
    """Health check endpoint with storage backend info."""
    try:
        from services.database_data_service import DatabaseDataService
        from core.config import is_demo_mode
        
        service = DatabaseDataService()
        backend_info = service.get_backend_info()
        
        return {
            "status": "healthy",
            "version": "1.0.0",
            "demo_mode": is_demo_mode(),
            "storage": {
                "backend": backend_info['backend'],
                "database_available": backend_info.get('database_available', False),
                "demo_mode": backend_info.get('demo_mode', False)
            }
        }
    except Exception as e:
        logger.error(f"Health check error: {e}")
        return {
            "status": "healthy",
            "version": "1.0.0",
            "demo_mode": False,
            "storage": {
                "backend": "unknown",
                "error": str(e)
            }
        }

# Serve React static files in production
frontend_build_dir = Path(__file__).parent.parent / "frontend" / "build"
static_dir = frontend_build_dir / "static"

# Only mount static files if the build directory exists
# This prevents errors during development when frontend hasn't been built
if frontend_build_dir.exists() and static_dir.exists():
    try:
        app.mount("/static", StaticFiles(directory=static_dir), name="static")
        logger.info(f"Serving static files from: {static_dir}")
    except Exception as e:
        logger.warning(f"Failed to mount static files: {e}")
else:
    logger.info("Frontend build directory not found - static file serving disabled")
    logger.info(f"  Expected: {frontend_build_dir}")
    logger.info("  Run 'npm run build' in the frontend directory to enable production mode")

if frontend_build_dir.exists() and (frontend_build_dir / "index.html").exists():
    
    @app.get("/{full_path:path}")
    async def serve_react_app(full_path: str):
        """Serve React app for all non-API routes."""
        # Don't interfere with API routes
        if full_path.startswith("api/"):
            return {"error": "Not found"}, 404
        
        # Serve index.html for React routing
        index_file = frontend_build_dir / "index.html"
        if index_file.exists():
            return FileResponse(index_file)
        return {"error": "Frontend not built"}, 404

if __name__ == "__main__":
    import uvicorn
    
    logger.info("Starting Vigil SOC API server...")
    uvicorn.run(
        "backend.main:app",
        host="0.0.0.0",
        port=6987,
        reload=True,
        log_level="info"
    )

