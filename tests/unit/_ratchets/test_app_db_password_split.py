"""App processes must not hold the database owner's password.

vigil_app is SELECT/INSERT on the agent ledger. That only holds if the password
in an app process cannot also log in as the owner.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit

REPO = Path(__file__).resolve().parents[3]
APPLY_SCRIPTS = [
    REPO / "infra" / "docker" / "initdb" / "00_apply.sh",
    REPO / "clients" / "desktop" / "standalone" / "initdb" / "00_apply.sh",
]
COMPOSE = REPO / "infra" / "docker" / "docker-compose.yml"
CHART = REPO / "infra" / "helm" / "vigil"
APP_DEFAULT = "${POSTGRES_APP_PASSWORD:-vigil_app_password_change_me}"
OWNER_DEFAULT = "${POSTGRES_PASSWORD:-deeptempo_secure_password_change_me}"


def _pairs(env: object) -> dict[str, str]:
    if isinstance(env, dict):
        return {str(key): str(value) for key, value in env.items()}
    if not isinstance(env, list):
        return {}
    out: dict[str, str] = {}
    for item in env:
        text = str(item)
        if "=" not in text:
            continue
        key, value = text.split("=", 1)
        out[key] = value
    return out


def test_compose_app_services_do_not_receive_the_owner_password() -> None:
    raw = yaml.safe_load(COMPOSE.read_text(encoding="utf-8")) or {}
    envs = {"x-agent-env": raw["x-agent-env"]}
    envs.update(
        {name: spec.get("environment") or {} for name, spec in raw["services"].items()}
    )
    app = 0
    owners = 0
    for name, env in envs.items():
        pairs = _pairs(env)
        user = pairs.get("POSTGRES_USER") or pairs.get("PGUSER")
        if user == "vigil_app":
            assert pairs.get("POSTGRES_PASSWORD") == APP_DEFAULT, name
            assert OWNER_DEFAULT not in pairs.values(), name
            app += 1
        if user == "deeptempo":
            owner_value = pairs.get("PGPASSWORD") or pairs.get("POSTGRES_PASSWORD")
            assert owner_value == OWNER_DEFAULT, name
            owners += 1
            if name in ("postgres", "upgrade"):
                assert pairs.get("POSTGRES_APP_PASSWORD") == APP_DEFAULT, name
    assert app >= 4, "agent anchor, backend, daemon and llm-worker"
    assert owners >= 3, "postgres, upgrade and backup"


@pytest.mark.parametrize("script", APPLY_SCRIPTS, ids=lambda path: path.parent.parent.name)
def test_apply_script_refuses_a_shared_or_missing_app_password(
    script: Path, tmp_path: Path
) -> None:
    missing = _apply(script, tmp_path / "missing", {})
    assert missing.returncode != 0
    assert "POSTGRES_APP_PASSWORD" in missing.stderr
    assert "--set=pw=" not in missing.stdout

    shared = _apply(
        script,
        tmp_path / "shared",
        {"POSTGRES_PASSWORD": "same-secret", "POSTGRES_APP_PASSWORD": "same-secret"},
    )
    assert shared.returncode != 0
    assert "must differ" in shared.stderr


@pytest.mark.parametrize("script", APPLY_SCRIPTS, ids=lambda path: path.parent.parent.name)
def test_apply_script_sets_vigil_app_from_its_own_password(
    script: Path, tmp_path: Path
) -> None:
    done = _apply(
        script,
        tmp_path / "ok",
        {
            "PGPASSWORD": "owner-secret",
            "POSTGRES_APP_PASSWORD": "app-secret",
        },
    )
    assert done.returncode == 0, done.stderr
    assert "--set=pw=app-secret" in (tmp_path / "ok" / "psql.log").read_text()
    assert "--set=pw=owner-secret" not in (tmp_path / "ok" / "psql.log").read_text()


def _apply(script: Path, tmp_path: Path, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    tmp_path.mkdir(parents=True)
    bindir = tmp_path / "bin"
    bindir.mkdir()
    log = tmp_path / "psql.log"
    psql = bindir / "psql"
    psql.write_text(
        "#!/bin/sh\n"
        f"printf '%s\\n' \"$*\" >> '{log}'\n"
        "cat >/dev/null\n"
        "exit 0\n",
        encoding="utf-8",
    )
    psql.chmod(0o755)
    return subprocess.run(
        ["sh", str(script)],
        capture_output=True,
        text=True,
        env={"PATH": f"{bindir}:/usr/bin:/bin", "HOME": str(tmp_path), **env},
        check=False,
    )


def test_helm_splits_the_owner_password_from_app_workloads(tmp_path: Path) -> None:
    if shutil.which("helm") is None:
        pytest.skip("needs helm")
    chart = tmp_path / "vigil"
    shutil.copytree(
        CHART, chart, ignore=shutil.ignore_patterns("charts", "medic-tests", "Chart.lock")
    )
    meta = yaml.safe_load((chart / "Chart.yaml").read_text(encoding="utf-8"))
    meta.pop("dependencies", None)
    (chart / "Chart.yaml").write_text(yaml.safe_dump(meta), encoding="utf-8")

    rendered = _helm(chart, "test-password", "test-app-password")
    docs = [doc for doc in yaml.safe_load_all(rendered) if isinstance(doc, dict)]
    secrets = {
        doc["metadata"]["name"]: doc["stringData"]
        for doc in docs
        if doc.get("kind") == "Secret" and "stringData" in doc
    }
    app_secrets = [data for data in secrets.values() if "POSTGRES_APP_PASSWORD" in data]
    owner_secrets = [
        data
        for data in secrets.values()
        if set(data) == {"POSTGRES_PASSWORD"}
    ]
    assert len(app_secrets) == 1
    assert "POSTGRES_PASSWORD" not in app_secrets[0]
    assert app_secrets[0]["POSTGRES_APP_PASSWORD"] == "test-app-password"
    assert owner_secrets == [{"POSTGRES_PASSWORD": "test-password"}]
    app_secret_name = next(
        name for name, data in secrets.items() if "POSTGRES_APP_PASSWORD" in data
    )
    owner_secret_name = next(
        name for name, data in secrets.items() if set(data) == {"POSTGRES_PASSWORD"}
    )
    assert app_secret_name != owner_secret_name

    app_refs: set[tuple[str, str]] = set()
    owner_refs: set[tuple[str, str]] = set()
    for doc in docs:
        pod = ((doc.get("spec") or {}).get("template") or {}).get("spec") or {}
        for container in pod.get("containers") or []:
            for entry in container.get("env") or []:
                ref = (entry.get("valueFrom") or {}).get("secretKeyRef") or {}
                if entry["name"] == "POSTGRES_PASSWORD":
                    app_refs.add((ref.get("name", ""), ref.get("key", "")))
                if entry["name"] in ("PGPASSWORD", "POSTGRES_PASSWORD") and ref.get(
                    "name"
                ) == owner_secret_name:
                    owner_refs.add((entry["name"], ref.get("key", "")))
    assert (app_secret_name, "POSTGRES_APP_PASSWORD") in app_refs
    assert all(key == "POSTGRES_APP_PASSWORD" for _, key in app_refs if _ == app_secret_name)
    assert (owner_secret_name, "POSTGRES_PASSWORD") in app_refs
    assert ("PGPASSWORD", "POSTGRES_PASSWORD") in owner_refs
    assert owner_secret_name not in {
        name for name, _key in app_refs if _key == "POSTGRES_APP_PASSWORD"
    }

    refused = subprocess.run(
        [
            "helm",
            "template",
            "release",
            str(chart),
            "--set",
            "secrets.postgresPassword=same",
            "--set",
            "secrets.vigilAppPassword=same",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert refused.returncode != 0
    assert "must differ" in refused.stderr


def _helm(chart: Path, owner: str, app: str) -> str:
    done = subprocess.run(
        [
            "helm",
            "template",
            "release",
            str(chart),
            "--set",
            f"secrets.postgresPassword={owner}",
            "--set",
            f"secrets.vigilAppPassword={app}",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    return done.stdout
