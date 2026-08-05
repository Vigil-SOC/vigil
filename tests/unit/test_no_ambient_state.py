import ast
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
PACKAGES = ("backend", "services", "daemon", "core", "database")

# The secrets manager implements the environment backend, so reading os.environ
# is its whole job. Every other boundary is exempted inline with '# noqa: ENV001'.
ENV_EXEMPT_FILES = {
    "backend/secrets_manager.py",
}

# Existing module-level instantiations. Converting these to accessor calls is
# import-timing churn across many handlers, so they are grandfathered by name.
SINGLETON_ALLOWED = {
    ("backend/api/agents.py", "agent_manager"),
    ("backend/api/analytics.py", "ai_insights_service"),
    ("backend/api/attack.py", "data_service"),
    ("backend/api/case_metrics.py", "metrics_service"),
    ("backend/api/case_search.py", "search_service"),
    ("backend/api/case_templates.py", "workflow_service"),
    ("backend/api/cases.py", "data_service"),
    ("backend/api/custom_agents.py", "service"),
    ("backend/api/findings.py", "data_service"),
    ("backend/api/vstrike.py", "data_service"),
    ("services/case_automation_service.py", "automation_service"),
    ("services/ingestion_jobs.py", "_registry"),
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
        if isinstance(node, ast.Attribute) and node.attr in ("getenv", "environ"):
            if not (isinstance(node.value, ast.Name) and node.value.id == "os"):
                continue
        elif isinstance(node, ast.ImportFrom) and node.module == "os":
            if not any(a.name in ("getenv", "environ") for a in node.names):
                continue
        else:
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


@pytest.mark.unit
def test_no_raw_env_reads():
    violations = []
    for rel_path in _python_files():
        if rel_path.as_posix() in ENV_EXEMPT_FILES:
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
        "Module-level service instantiation found. Build the instance inside a "
        "get_*() accessor so import order and test isolation stay predictable.\n"
        + "\n".join(violations)
    )
