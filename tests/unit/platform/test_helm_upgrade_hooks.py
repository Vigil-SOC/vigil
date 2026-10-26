"""Helm runs ``python -m core.upgrade`` as hooks (#2739), rendered.

Order: everything the Jobs need at -20, ``--check`` at -10, the stop-and-upgrade
at 0, and the install Job after install. Only the weight-0 Job holds a token,
and its Role is namespaced. The -10 Job runs the check and nothing that scales.
Rendered on a copy of the chart without its subcharts, as the Medic render tests
are, so it needs the helm binary but no network.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

pytestmark = [
    pytest.mark.unit,
    pytest.mark.skipif(shutil.which("helm") is None, reason="needs helm"),
]

CHART = Path(__file__).resolve().parents[3] / "infra" / "helm" / "vigil"
FN = "t-vigil-upgrade"
BASE = {
    "secrets.postgresPassword": "test-password",
    "secrets.vigilAppPassword": "test-app-password",
}
POLICIES_ON = {
    "networkPolicies.enabled": "true",
    "medic.kubeApi.cidrs[0]": "10.0.0.1/32",
}


@pytest.fixture(scope="module")
def chart(tmp_path_factory) -> Path:
    dest = tmp_path_factory.mktemp("chart") / "vigil"
    shutil.copytree(CHART, dest, ignore=shutil.ignore_patterns("medic-tests"))
    meta = yaml.safe_load((dest / "Chart.yaml").read_text())
    meta.pop("dependencies", None)
    (dest / "Chart.yaml").write_text(yaml.safe_dump(meta))
    (dest / "Chart.lock").unlink(missing_ok=True)
    return dest


def _render(chart: Path, **sets: str) -> list[dict]:
    args = ["helm", "template", "t", str(chart)]
    for k, v in {**BASE, **sets}.items():
        args += ["--set", f"{k}={v}"]
    done = subprocess.run(args, capture_output=True, text=True, check=False)
    assert done.returncode == 0, done.stderr
    return [d for d in yaml.safe_load_all(done.stdout) if d]


@pytest.fixture(scope="module")
def docs(chart) -> list[dict]:
    return _render(chart, **POLICIES_ON)


def _hooks(docs) -> dict[tuple[str, str], tuple[str, str]]:
    return {
        (d["kind"], d["metadata"]["name"]): (
            d["metadata"]["annotations"]["helm.sh/hook"],
            d["metadata"]["annotations"].get("helm.sh/hook-weight", "0"),
        )
        for d in docs
        if "helm.sh/hook" in (d["metadata"].get("annotations") or {})
        and d["metadata"]["name"].startswith(FN)
    }


def _job(docs, name: str) -> dict:
    return next(
        d
        for d in docs
        if d["kind"] == "Job" and d["metadata"]["name"] == f"{FN}-{name}"
    )


def _pod(doc) -> dict:
    return doc["spec"]["template"]["spec"]


def test_hook_order(docs) -> None:
    setup = ("pre-install,pre-upgrade", "-20")
    assert _hooks(docs) == {
        ("ServiceAccount", FN): setup,
        ("Role", FN): setup,
        ("RoleBinding", FN): setup,
        ("ConfigMap", FN): setup,
        ("PersistentVolumeClaim", FN): setup,
        ("NetworkPolicy", FN): setup,
        ("NetworkPolicy", f"{FN}-postgres"): setup,
        ("Job", f"{FN}-check"): ("pre-upgrade", "-10"),
        ("Job", f"{FN}-apply"): ("pre-upgrade", "0"),
        ("Job", f"{FN}-install"): ("post-install", "0"),
    }


def test_the_db_init_job_is_gone(docs) -> None:
    assert not [d for d in docs if "db-init" in d["metadata"]["name"]]
    assert not (CHART / "files" / "database-init").exists()


def test_the_pvc_survives_upgrades(docs) -> None:
    pvc = next(
        d
        for d in docs
        if d["kind"] == "PersistentVolumeClaim" and d["metadata"]["name"] == FN
    )
    notes = pvc["metadata"]["annotations"]
    assert notes["helm.sh/resource-policy"] == "keep"
    # A delete policy would take history.jsonl with it.
    assert "helm.sh/hook-delete-policy" not in notes
    assert pvc["spec"]["resources"]["requests"]["storage"] == "1Gi"


def test_nothing_cluster_scoped(docs) -> None:
    assert not [d for d in docs if d["kind"] in ("ClusterRole", "ClusterRoleBinding")]
    binding = next(
        d for d in docs if d["kind"] == "RoleBinding" and d["metadata"]["name"] == FN
    )
    assert binding["roleRef"]["kind"] == "Role"


def test_the_role_can_scale_and_pause_nothing_more(docs) -> None:
    role = next(d for d in docs if d["kind"] == "Role" and d["metadata"]["name"] == FN)
    writes = {
        r
        for rule in role["rules"]
        if "patch" in rule["verbs"]
        for r in rule["resources"]
    }
    assert writes == {"deployments/scale", "statefulsets/scale", "scaledobjects"}
    verbs = {v for rule in role["rules"] for v in rule["verbs"]}
    assert verbs == {"get", "list", "patch"}


def test_only_the_apply_job_holds_a_token(docs) -> None:
    sa = next(
        d for d in docs if d["kind"] == "ServiceAccount" and d["metadata"]["name"] == FN
    )
    assert sa["automountServiceAccountToken"] is True
    app_sa = next(
        d
        for d in docs
        if d["kind"] == "ServiceAccount" and d["metadata"]["name"] == "t-vigil"
    )
    assert app_sa["automountServiceAccountToken"] is False
    for d in docs:
        if d["kind"] not in ("Deployment", "StatefulSet", "Job"):
            continue
        pod = _pod(d)
        if pod.get("serviceAccountName") == FN:
            assert pod["automountServiceAccountToken"] is (
                d["metadata"]["name"] == f"{FN}-apply"
            )


def test_the_pvc_is_on_the_upgrade_jobs_only(docs) -> None:
    for d in docs:
        if d["kind"] not in ("Deployment", "StatefulSet", "Job"):
            continue
        claims = {
            v["persistentVolumeClaim"]["claimName"]
            for v in _pod(d).get("volumes", [])
            if "persistentVolumeClaim" in v
        }
        upgrade_job = d["kind"] == "Job" and d["metadata"]["name"].startswith(FN)
        assert (FN in claims) is upgrade_job, d["metadata"]["name"]
        if upgrade_job:
            env = {e["name"]: e.get("value") for e in _pod(d)["containers"][0]["env"]}
            assert env["VIGIL_UPGRADE_DIR"] == "/var/lib/vigil-upgrade"


def test_the_check_runs_check_and_never_scales(docs) -> None:
    pod = _pod(_job(docs, "check"))
    script = " ".join(pod["containers"][0]["command"])
    assert script.rstrip().endswith("exec python -m core.upgrade --check")
    assert "helm_upgrade" not in script and "scale" not in script
    assert not any("configMap" in v for v in pod["volumes"])  # no script mounted
    assert _job(docs, "check")["spec"]["backoffLimit"] == 0


def test_jobs_log_in_as_the_owner_with_the_acknowledgement(chart) -> None:
    docs = _render(
        chart,
        **{
            "upgrade.acknowledgeVersion": "9.9.9",
            "upgrade.acknowledgeReason": "snapshot 42",
        },
    )
    for name in ("check", "apply", "install"):
        env = {e["name"]: e for e in _pod(_job(docs, name))["containers"][0]["env"]}
        assert env["POSTGRES_USER"]["value"] == "deeptempo"
        assert (
            env["POSTGRES_PASSWORD"]["valueFrom"]["secretKeyRef"]["name"]
            == "t-vigil-postgres"
        )
        assert (
            env["POSTGRES_APP_PASSWORD"]["valueFrom"]["secretKeyRef"]["key"]
            == "POSTGRES_APP_PASSWORD"
        )
        assert env["VIGIL_UPGRADE_ACK_VERSION"]["value"] == "9.9.9"
        assert env["VIGIL_UPGRADE_ACK_REASON"]["value"] == "snapshot 42"
        assert env["VIGIL_CHART_APP_VERSION"]["value"] == str(
            yaml.safe_load((CHART / "Chart.yaml").read_text())["appVersion"]
        )


def test_upgrade_pods_dodge_the_catch_all_and_reach_postgres(docs) -> None:
    labels = _job(docs, "apply")["spec"]["template"]["metadata"]["labels"]
    assert labels["app.kubernetes.io/name"] == "vigil-upgrade"  # not the chart's pair
    own = next(
        d for d in docs if d["kind"] == "NetworkPolicy" and d["metadata"]["name"] == FN
    )
    for rule in own["spec"]["egress"]:
        assert rule.get("ports"), "an egress rule with no ports allows every port"
    assert {"ipBlock": {"cidr": "10.0.0.1/32"}} in [
        p for r in own["spec"]["egress"] for p in r.get("to", [])
    ]
    pg = next(
        d
        for d in docs
        if d["kind"] == "NetworkPolicy" and d["metadata"]["name"] == f"{FN}-postgres"
    )
    peer = pg["spec"]["ingress"][0]["from"][0]["podSelector"]["matchLabels"]
    assert peer["app.kubernetes.io/component"] == "upgrade"


@pytest.mark.parametrize("key", ["backend", "llmWorker", "agentWorker"])
def test_autoscaled_workloads_name_a_replica_count(chart, key: str) -> None:
    # The apply Job scales them to 0; Helm only scales back what the manifest names.
    docs = _render(
        chart,
        **{f"{key}.autoscaling.enabled": "true", f"{key}.autoscaling.minReplicas": "3"},
    )
    names = {
        "backend": "t-vigil-backend",
        "llmWorker": "t-vigil-llm-worker",
        "agentWorker": "t-vigil-agent-worker",
    }
    dep = next(
        d
        for d in docs
        if d["kind"] == "Deployment" and d["metadata"]["name"] == names[key]
    )
    assert dep["spec"]["replicas"] == 3


def test_disabled_renders_no_upgrade_object(chart) -> None:
    docs = _render(chart, **{"upgrade.enabled": "false"})
    assert not [d for d in docs if d["metadata"]["name"].startswith(FN)]
