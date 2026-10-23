"""The Helm upgrade Job's script against a fake Kubernetes API and a real Postgres.

``infra/helm/vigil/files/upgrade/helm_upgrade.py`` stops the release, runs
``python -m core.upgrade`` and either leaves the scaling to Helm or puts back
what it recorded. The API is a stdlib HTTP server that keeps replica counts and
ScaledObject annotations, and serves pods for every workload above 0. It does
not apply label selectors, so the script's own filter has to leave Postgres out.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import psycopg2
import pytest
from sqlalchemy import text

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

pytestmark = [pytest.mark.integration, pytest.mark.database]

from core.version import __version__  # noqa: E402
from scripts.generate_schema_snapshot import (  # noqa: E402
    ScratchDatabaseUnavailable,
    postgres_skip_reason,
    scratch_database,
)

SCRIPT = REPO / "infra" / "helm" / "vigil" / "files" / "upgrade" / "helm_upgrade.py"
NS, RELEASE = "ns", "rel"
PAUSE = "autoscaling.keda.sh/paused-replicas"
APP_PASSWORD = "helm-upgrade-test-app"


class FakeKube:
    """Deployments and StatefulSets with scale, ScaledObjects, and their pods."""

    def __init__(self) -> None:
        self.workloads = {
            ("deployments", "rel-vigil-backend"): ["backend", 2],
            ("deployments", "rel-vigil-llm-worker"): ["llm-worker", 2],
            ("statefulsets", "rel-vigil-daemon"): ["daemon", 1],
            ("statefulsets", "rel-vigil-postgres"): ["postgres", 1],
        }
        self.scaledobjects = {
            "rel-vigil-llm-worker": {
                "target": "rel-vigil-llm-worker",
                "annotations": {},
            },
            "someone-else": {"target": "not-ours", "annotations": {PAUSE: "5"}},
        }
        self.requests: list[tuple[str, str, object]] = []

    def labels(self, component: str) -> dict:
        return {
            "app.kubernetes.io/instance": RELEASE,
            "app.kubernetes.io/component": component,
        }

    def handle(self, method: str, url: str, body):
        self.requests.append((method, url, body))
        path = urlparse(url).path.strip("/").split("/")
        if path[:4] == ["apis", "apps", "v1", "namespaces"]:
            plural, name = path[5], path[6] if len(path) > 6 else None
            if name is None:
                return {
                    "items": [
                        {
                            "metadata": {"name": n, "labels": self.labels(c)},
                            "spec": {"selector": {"matchLabels": self.labels(c)}},
                        }
                        for (p, n), (c, _) in self.workloads.items()
                        if p == plural
                    ]
                }
            if method == "PATCH":
                self.workloads[(plural, name)][1] = body["spec"]["replicas"]
            return {"spec": {"replicas": self.workloads[(plural, name)][1]}}
        if path[:2] == ["apis", "keda.sh"]:
            if len(path) == 6:
                return {
                    "items": [
                        {
                            "metadata": {
                                "name": n,
                                "annotations": dict(so["annotations"]),
                            },
                            "spec": {"scaleTargetRef": {"name": so["target"]}},
                        }
                        for n, so in self.scaledobjects.items()
                    ]
                }
            annotations = self.scaledobjects[path[6]]["annotations"]
            for k, v in body["metadata"]["annotations"].items():
                if v is None:
                    annotations.pop(k, None)
                else:
                    annotations[k] = v
            return {}
        if path[-1] == "pods":
            selector = dict(
                kv.split("=", 1)
                for kv in parse_qs(urlparse(url).query)["labelSelector"][0].split(",")
            )
            return {
                "items": [
                    {"metadata": {"name": f"{n}-{i}"}}
                    for (_, n), (c, replicas) in self.workloads.items()
                    for i in range(replicas)
                    if self.labels(c).items() >= selector.items()
                ]
            }
        return None

    def replicas(self) -> dict[str, int]:
        return {n: r for (_, n), (_, r) in self.workloads.items()}

    def patched(self) -> list[str]:
        return [url for method, url, _ in self.requests if method == "PATCH"]


@contextmanager
def fake_api():
    kube = FakeKube()

    class Handler(BaseHTTPRequestHandler):
        def _serve(self):
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length)) if length else None
            found = kube.handle(self.command, self.path, body)
            payload = json.dumps(found or {"kind": "Status", "code": 404}).encode()
            self.send_response(200 if found is not None else 404)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        do_GET = do_PATCH = _serve

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield kube, f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()


@contextmanager
def _scratch():
    reason = postgres_skip_reason()
    if reason:
        pytest.skip(reason)
    try:
        with scratch_database(provision=False) as (name, engine):
            yield name, engine
    except ScratchDatabaseUnavailable as e:
        pytest.skip(str(e))


def _run(
    mode: str, engine, api: str, tmp_path: Path, **env
) -> subprocess.CompletedProcess:
    sa = tmp_path / "sa"
    sa.mkdir(exist_ok=True)
    (sa / "token").write_text("test-token")
    url = engine.url
    full = {
        **os.environ,
        "POSTGRES_HOST": url.host,
        "POSTGRES_PORT": str(url.port or 5432),
        "POSTGRES_DB": url.database,
        "POSTGRES_USER": url.username,
        "POSTGRES_PASSWORD": url.password,
        "POSTGRES_APP_PASSWORD": APP_PASSWORD,
        "VIGIL_KUBE_API": api,
        "VIGIL_KUBE_SA_DIR": str(sa),
        "VIGIL_NAMESPACE": NS,
        "VIGIL_RELEASE": RELEASE,
        "VIGIL_UPGRADE_DIR": str(tmp_path / "upgrade"),
        "VIGIL_UPGRADE_STOP_TIMEOUT": "20",
        "VIGIL_CHART_APP_VERSION": __version__,
        "VIGIL_UPGRADE_ACK_VERSION": __version__,
        "VIGIL_UPGRADE_ACK_REASON": "test: scratch database",
        **env,
    }
    return subprocess.run(
        [sys.executable, str(SCRIPT), mode],
        cwd=REPO,
        env=full,
        capture_output=True,
        text=True,
        timeout=300,
    )


def test_a_stale_acknowledgement_stops_nothing(tmp_path) -> None:
    with _scratch() as (_, engine), fake_api() as (kube, api):
        done = _run("apply", engine, api, tmp_path, VIGIL_UPGRADE_ACK_VERSION="0.0.1")
    assert done.returncode == 1, done.stdout + done.stderr
    assert "refused" in done.stdout and "Nothing was stopped" in done.stdout
    assert kube.requests == []


def test_a_refusal_with_alembic_version_unchanged_restores(tmp_path) -> None:
    with _scratch() as (_, engine), fake_api() as (kube, api):
        # Tables but no alembic_version and no schema_version: the command refuses.
        with engine.begin() as conn:
            conn.execute(text("CREATE TABLE stray (id int)"))
        before = kube.replicas()
        done = _run("apply", engine, api, tmp_path)

    assert done.returncode == 1, done.stdout + done.stderr
    assert "restoring" in done.stdout
    # Every workload went to 0 and came back; Postgres was never touched.
    zeros = [
        u
        for m, u, b in kube.requests
        if m == "PATCH" and b == {"spec": {"replicas": 0}}
    ]
    assert len(zeros) == 3 and not any("postgres" in u for u in kube.patched())
    assert kube.replicas() == before
    assert kube.scaledobjects["rel-vigil-llm-worker"]["annotations"] == {}
    assert kube.scaledobjects["someone-else"]["annotations"] == {PAUSE: "5"}
    assert not any("someone-else" in u for u in kube.patched())
    assert not (tmp_path / "upgrade" / "helm-scale.json").exists()


def test_install_then_apply_ends_app_sessions_and_leaves_scaling_to_helm(
    tmp_path,
) -> None:
    with _scratch() as (name, engine), fake_api() as (kube, api):
        done = _run("install", engine, api, tmp_path)
        assert done.returncode == 0, done.stdout + done.stderr
        assert kube.requests == []  # the install Job never calls the API
        history = (tmp_path / "upgrade" / "history.jsonl").read_text().splitlines()
        assert json.loads(history[-1])["outcome"] == "applied"

        url = engine.url
        app = psycopg2.connect(
            host=url.host,
            port=url.port or 5432,
            dbname=name,
            user="vigil_app",
            password=APP_PASSWORD,
        )
        try:
            with app.cursor() as cur:
                cur.execute("SELECT version_num FROM alembic_version")
                assert cur.fetchone()

            done = _run("apply", engine, api, tmp_path)
            assert done.returncode == 0, done.stdout + done.stderr
            assert "terminated 1 vigil_app session(s)" in done.stdout
            with pytest.raises(psycopg2.Error), app.cursor() as cur:
                cur.execute("SELECT 1")
        finally:
            app.close()

    # Down to 0 and left there for Helm's manifests; KEDA un-paused.
    assert kube.replicas() == {
        "rel-vigil-backend": 0,
        "rel-vigil-llm-worker": 0,
        "rel-vigil-daemon": 0,
        "rel-vigil-postgres": 1,
    }
    assert kube.scaledobjects["rel-vigil-llm-worker"]["annotations"] == {}
    assert (
        "PATCH",
        "/apis/keda.sh/v1alpha1/namespaces/ns/scaledobjects/rel-vigil-llm-worker",
        {"metadata": {"annotations": {PAUSE: "0"}}},
    ) in kube.requests
