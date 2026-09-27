import ast
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
PACKAGES = ("services", "core")

# Files where reading os.environ is the point, not a violation. The secrets
# manager implements the environment backend; mcp_service exports config into
# spawned MCP child processes, whose config protocol *is* env vars.
ENV_EXEMPT_FILES = {
    "core/secrets_manager.py",
    # Tool provider for the security-detections MCP server. Its rule-corpus
    # paths arrive as env from the process that spawns that server, so it sits
    # on the same boundary as ENV_EXEMPT_GLOBS below.
    "core/detections/tools.py",
}

# The other side of that boundary: core/integrations/*/tool.py are standalone
# MCP servers, spawned as child processes whose config arrives only as env.
ENV_EXEMPT_GLOBS = ("core/integrations/*/tool.py",)

# Existing module-level instantiations. Converting these to accessor calls is
# import-timing churn across many handlers, so they are grandfathered by name.
SINGLETON_ALLOWED = {
    ("services/api/routers/agents.py", "agent_manager"),
    ("services/api/routers/analytics.py", "ai_insights_service"),
    ("core/threat_intel/attack_router.py", "data_service"),
    ("core/api/v1/case_metrics_router.py", "metrics_service"),
    ("core/api/v1/cases_router.py", "data_service"),
    ("core/api/v1/findings_router.py", "data_service"),
    ("core/cases/case_search_router.py", "search_service"),
    ("core/cases/case_templates_router.py", "workflow_service"),
    ("services/api/routers/cases.py", "data_service"),
    ("services/api/routers/custom_agents.py", "service"),
    ("services/api/routers/findings.py", "data_service"),
    ("services/api/routers/vstrike.py", "data_service"),
    ("core/cases/case_automation_service.py", "automation_service"),
    ("core/ingestion/ingestion_jobs.py", "_registry"),
}


# Accessors that may keep caching one instance in a module global. The first two
# are process-scoped resources (DatabaseManager owns the connection pool,
# IngestionJobRegistry holds in-flight job state); the rest are config/secrets
# channels and standalone MCP tool processes, not injectable services. The 13
# service singletons #459 retired are deliberately absent — do not add
# more without a process-scoped resource justification.
LAZY_SINGLETON_ALLOWED = {
    ("core/storage/connection.py", "get_db_manager"),
    ("core/ingestion/ingestion_jobs.py", "get_job_registry"),
    ("core/secrets_manager.py", "get_secrets_manager"),
    ("core/storage/config_service.py", "get_config_service"),
    ("core/llm/providers/registry.py", "get_registry"),
    ("core/integrations/elastic/tool.py", "get_elastic_service"),
    ("core/integrations/splunk/tool.py", "get_splunk_service"),
    ("core/integrations/vstrike/client.py", "get_vstrike_service"),
    # Module-private and already injectable: every caller may pass its own
    # ``data_service``, and this only defers the connection for those that don't.
    ("core/findings/enrichment/service.py", "_default_data_service"),
}


SERVICE_SUFFIXES = ("Service", "Registry", "Manager", "Client")

# Not a service: the FastAPI router and the SQLAlchemy/limiter primitives are
# module-level by design in every FastAPI codebase.
SINGLETON_IGNORED_CALLEES = {"APIRouter", "Limiter", "HTTPBearer", "Path"}


def _python_files():
    for package in PACKAGES:
        for path in sorted((REPO_ROOT / package).rglob("*.py")):
            if "__pycache__" in path.parts:
                continue
            yield path.relative_to(REPO_ROOT)


def _parse(rel_path: Path):
    source = (REPO_ROOT / rel_path).read_text(encoding="utf-8")
    return source.splitlines(), ast.parse(source)


def _callee_name(node: ast.AST):
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def _env_reads(rel_path: Path):
    lines, tree = _parse(rel_path)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Attribute) or node.attr not in ("getenv", "environ"):
            continue
        if not (isinstance(node.value, ast.Name) and node.value.id == "os"):
            continue
        if "noqa: ENV001" in lines[node.lineno - 1]:
            continue
        yield node.lineno, lines[node.lineno - 1].strip()


def _module_level_services(rel_path: Path):
    _, tree = _parse(rel_path)
    for node in tree.body:
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        if not isinstance(node.value, ast.Call):
            continue
        callee = _callee_name(node.value.func)
        if not callee or callee in SINGLETON_IGNORED_CALLEES:
            continue
        if not callee.endswith(SERVICE_SUFFIXES):
            continue
        targets = [node.target] if isinstance(node, ast.AnnAssign) else node.targets
        for target in targets:
            if isinstance(target, ast.Name):
                yield node.lineno, target.id, callee


def _lazy_singleton_accessors(rel_path: Path):
    _, tree = _parse(rel_path)
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        declared = {
            name
            for stmt in ast.walk(node)
            if isinstance(stmt, ast.Global)
            for name in stmt.names
        }
        if not declared:
            continue
        for stmt in ast.walk(node):
            if not isinstance(stmt, ast.Assign) or not isinstance(stmt.value, ast.Call):
                continue
            callee = _callee_name(stmt.value.func)
            if not callee or not callee.endswith(SERVICE_SUFFIXES):
                continue
            if any(isinstance(t, ast.Name) and t.id in declared for t in stmt.targets):
                yield node.lineno, node.name, callee


@pytest.mark.unit
def test_no_raw_env_reads():
    violations = []
    for rel_path in _python_files():
        if rel_path.as_posix() in ENV_EXEMPT_FILES:
            continue
        if any(rel_path.match(glob) for glob in ENV_EXEMPT_GLOBS):
            continue
        for lineno, text in _env_reads(rel_path):
            violations.append(f"{rel_path}:{lineno}: {text}")
    assert not violations, (
        "Raw environment reads found. Use core.config.get_settings() for config "
        "and get_secret() for credentials. If the read is a genuine process "
        "boundary, append '# noqa: ENV001' with a reason.\n" + "\n".join(violations)
    )


@pytest.mark.unit
def test_no_module_level_service_instantiation():
    violations = []
    for rel_path in _python_files():
        for lineno, name, callee in _module_level_services(rel_path):
            if (rel_path.as_posix(), name) in SINGLETON_ALLOWED:
                continue
            violations.append(f"{rel_path}:{lineno}: {name} = {callee}(...)")
    assert not violations, (
        "Module-level service instantiation found. Construct it in the "
        "services/api/main.py lifespan (onto app.state) and inject it with a "
        "core/deps.py provider instead, so tests can substitute a fake.\n"
        + "\n".join(violations)
    )


@pytest.mark.unit
def test_no_lazy_singleton_accessors():
    violations = []
    for rel_path in _python_files():
        for lineno, func, callee in _lazy_singleton_accessors(rel_path):
            if (rel_path.as_posix(), func) in LAZY_SINGLETON_ALLOWED:
                continue
            violations.append(f"{rel_path}:{lineno}: {func}() caches {callee}(...)")
    assert not violations, (
        "Lazy module-global singleton accessor found — the shape #459 retired. "
        "A module global wired to a real DB/LLM/MCP connection cannot be faked "
        "in a test. Construct the service in the services/api/main.py lifespan "
        "(or the daemon's _init_components) and inject it: FastAPI handlers via "
        "a core/deps.py provider, everything else via a constructor keyword "
        "argument.\n" + "\n".join(violations)
    )
