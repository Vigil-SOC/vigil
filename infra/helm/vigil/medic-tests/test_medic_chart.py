"""Medic on Helm, rendered: C3 §7 checks 2 and 15 (template half), C4, C5, C7, C8.

Run: `uv run --project services/medic pytest infra/helm/vigil/medic-tests`
(needs `helm`). The live half (checks 3, 14, 15 and the S2-6 volume test) runs on
kind in `.github/workflows/medic-helm.yml`.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

CHART = Path(__file__).resolve().parents[1]
RELEASE = "rel"
NAMESPACE = "ns"
KUBE_API = "172.18.0.2/32"

# The least a Medic install needs: everything else has a default.
MEDIC_ON = {
    "medic.enabled": "true",
    "medic.kubeApi.cidrs[0]": KUBE_API,
    "medic.gateway.viewer.username": "medic-viewer",
    "medic.gateway.viewer.passwordSecret.name": "medic-viewer",
}


@pytest.fixture(scope="session")
def chart(tmp_path_factory) -> Path:
    if shutil.which("helm") is None:
        pytest.skip("helm isn't installed")
    dest = tmp_path_factory.mktemp("chart") / "vigil"
    shutil.copytree(CHART, dest, ignore=shutil.ignore_patterns("medic-tests"))
    meta = yaml.safe_load((dest / "Chart.yaml").read_text())
    meta.pop("dependencies", None)
    (dest / "Chart.yaml").write_text(yaml.safe_dump(meta))
    (dest / "Chart.lock").unlink(missing_ok=True)
    return dest


class RenderError(Exception):
    pass


def helm_template(chart: Path, values: dict[str, str]) -> list[dict]:
    args = ["helm", "template", RELEASE, str(chart), "--namespace", NAMESPACE]
    for key, value in values.items():
        args += ["--set", f"{key}={value}"]
    proc = subprocess.run(args, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        raise RenderError(proc.stderr)
    return [d for d in yaml.safe_load_all(proc.stdout) if d]


@pytest.fixture(scope="session")
def render(chart):
    cache: dict[tuple, list[dict]] = {}

    def _render(values: dict[str, str] | None = None) -> list[dict]:
        values = values or {}
        key = tuple(sorted(values.items()))
        if key not in cache:
            cache[key] = helm_template(chart, values)
        return cache[key]

    return _render


REPO = Path(__file__).resolve().parents[4]
SECRET_NAMES = {
    line.strip()
    for line in (REPO / "scripts/vigil-support/secret-names.txt")
    .read_text()
    .splitlines()
    if line.strip() and not line.startswith("#")
}
AGENT_TOKENS = {"AGENT_INTERNAL_TOKEN", "VIGIL_TOOLS_TOKEN", "DAEMON_WEBHOOK_TOKEN"}
WORKLOADS = {"Deployment", "StatefulSet", "Job", "Pod", "DaemonSet"}
# Everything on, with the chart's own policies on: the worst case for the label trap.
ALL_ON = {
    **MEDIC_ON,
    "networkPolicies.enabled": "true",
    "splunk.enabled": "true",
    "pgadmin.enabled": "true",
}


def kinds(docs, kind):
    return [d for d in docs if d["kind"] == kind]


def named(docs, kind, name):
    found = [d for d in docs if d["kind"] == kind and d["metadata"]["name"] == name]
    assert len(found) == 1, f"{kind}/{name}: {len(found)} found"
    return found[0]


def pod_spec(doc):
    spec = doc["spec"]
    if doc["kind"] == "Pod":
        return spec
    if doc["kind"] == "CronJob":
        return spec["jobTemplate"]["spec"]["template"]["spec"]
    return spec["template"]["spec"]


def pod_labels(doc):
    return doc["spec"]["template"]["metadata"]["labels"]


def containers(spec):
    return spec.get("initContainers", []) + spec["containers"]


def selects(selector: dict, labels: dict) -> bool:
    """Kubernetes label-selector semantics; an empty selector selects every pod."""
    for k, v in (selector.get("matchLabels") or {}).items():
        if labels.get(k) != v:
            return False
    for expr in selector.get("matchExpressions") or []:
        key, op, vals = expr["key"], expr["operator"], expr.get("values", [])
        if op == "In" and labels.get(key) not in vals:
            return False
        if op == "NotIn" and labels.get(key) in vals:
            return False
        if op == "Exists" and key not in labels:
            return False
        if op == "DoesNotExist" and key in labels:
            return False
    return True


MEDIC = "rel-vigil-medic"
GATEWAY = "rel-vigil-medic-gateway"
OWN_POLICIES = {f"{MEDIC}-deny-all", MEDIC, GATEWAY}


@pytest.fixture(scope="module")
def on(render):
    return render(MEDIC_ON)


@pytest.fixture(scope="module")
def all_on(render):
    return render(ALL_ON)


def medic_pod(docs):
    return pod_spec(named(docs, "Deployment", MEDIC))


def gateway_pod(docs):
    return pod_spec(named(docs, "Deployment", GATEWAY))


# --- C8: off by default ---------------------------------------------------------


def test_disabled_by_default_renders_no_medic_object(render) -> None:
    docs = render()
    medic = [
        f"{d['kind']}/{d['metadata']['name']}"
        for d in docs
        if "medic" in d["metadata"]["name"]
        or "medic" in str(d["metadata"].get("labels"))
    ]
    assert medic == []


def test_disabled_needs_none_of_medics_required_values(render) -> None:
    render({"medic.enabled": "false"})  # no cidrs, no viewer: still renders


def test_enabled_renders_the_medic_set(on) -> None:
    names = {
        (d["kind"], d["metadata"]["name"])
        for d in on
        if "medic" in d["metadata"]["name"]
    }
    assert names == {
        ("ServiceAccount", MEDIC),
        ("ServiceAccount", GATEWAY),
        ("Role", MEDIC),
        ("RoleBinding", MEDIC),
        ("PersistentVolumeClaim", MEDIC),
        ("Deployment", MEDIC),
        ("Deployment", GATEWAY),
        ("Service", MEDIC),
        ("Service", GATEWAY),
        ("Service", f"{GATEWAY}-in"),
        ("Service", f"{MEDIC}-agent-worker"),
        ("NetworkPolicy", f"{MEDIC}-deny-all"),
        ("NetworkPolicy", MEDIC),
        ("NetworkPolicy", GATEWAY),
        ("Secret", f"{MEDIC}-api-key"),
    }


# --- C4 / C5 / C7: the Medic Deployment -----------------------------------------


def test_deployment_is_one_replica_recreate(on) -> None:
    dep = named(on, "Deployment", MEDIC)
    assert dep["spec"]["replicas"] == 1
    assert dep["spec"]["strategy"] == {"type": "Recreate"}


def test_medic_runs_as_10001_with_its_own_fsgroup(on) -> None:
    spec = medic_pod(on)
    sc = spec["securityContext"]
    assert sc["runAsUser"] == sc["runAsGroup"] == sc["fsGroup"] == 10001
    assert sc["runAsNonRoot"] is True
    assert sc["fsGroupChangePolicy"] == "OnRootMismatch"
    assert sc["seccompProfile"] == {"type": "RuntimeDefault"}
    (c,) = spec["containers"]
    csc = c["securityContext"]
    assert csc["readOnlyRootFilesystem"] is True
    assert csc["allowPrivilegeEscalation"] is False
    assert csc["capabilities"] == {"drop": ["ALL"]}
    assert "initContainers" not in spec  # no root chown container (C4 §9)


def test_medic_pvc_mounted_at_the_data_dir(on) -> None:
    spec = medic_pod(on)
    (c,) = spec["containers"]
    mount = next(
        m for m in c["volumeMounts"] if m["mountPath"] == "/var/lib/vigil-medic"
    )
    vol = next(v for v in spec["volumes"] if v["name"] == mount["name"])
    assert vol["persistentVolumeClaim"]["claimName"] == MEDIC
    pvc = named(on, "PersistentVolumeClaim", MEDIC)
    assert pvc["spec"]["resources"]["requests"]["storage"] == "2Gi"
    assert pvc["spec"]["accessModes"] == ["ReadWriteOnce"]
    # `helm uninstall` leaves the store (C4 §6.8, K4).
    assert pvc["metadata"]["annotations"]["helm.sh/resource-policy"] == "keep"


def test_persistence_off_uses_emptydir(render) -> None:
    docs = render({**MEDIC_ON, "medic.persistence.enabled": "false"})
    assert not [
        d
        for d in kinds(docs, "PersistentVolumeClaim")
        if "medic" in d["metadata"]["name"]
    ]
    vol = next(v for v in medic_pod(docs)["volumes"] if v["name"] == "data")
    assert vol == {"name": "data", "emptyDir": {}}


def test_medic_limits_are_c7s(on) -> None:
    (c,) = medic_pod(on)["containers"]
    assert c["resources"] == {
        "requests": {"cpu": "250m", "memory": "256Mi"},
        "limits": {"cpu": "1000m", "memory": "1Gi"},
    }


def test_medic_probes_call_check(on) -> None:
    (c,) = medic_pod(on)["containers"]
    check = ["python", "-m", "services.medic", "check"]
    # C5 §5.2: startup 10 s × 30, liveness 30 s × 4, readiness 30 s × 2.
    # Readiness is `check --ready` (C5: not Ready only while the API can't
    # serve), so the gateway's Service sends the backend's poll nowhere else.
    for probe, period, failures, command in (
        ("startupProbe", 10, 30, check),
        ("livenessProbe", 30, 4, check),
        ("readinessProbe", 30, 2, [*check, "--ready"]),
    ):
        p = c[probe]
        assert p["exec"]["command"] == command, probe
        assert (p["periodSeconds"], p["failureThreshold"]) == (period, failures), probe


def test_medic_env_names_its_shape_and_targets(on) -> None:
    (c,) = medic_pod(on)["containers"]
    env = {e["name"]: e.get("value") for e in c["env"]}
    assert env["VIGIL_MEDIC_ENABLED"] == "true"
    assert env["VIGIL_MEDIC_INSTALL_SHAPE"] == "helm"
    # A subdirectory Medic creates itself (0700, its uid): a volume root is never
    # private enough for the store (S7-1: hostPath ignores fsGroup, and fsGroup
    # only adds group bits, never clears "other").
    assert env["VIGIL_MEDIC_DATA_DIR"] == "/var/lib/vigil-medic/data"
    assert env["VIGIL_MEDIC_AGENT_WORKER_ADDR"] == f"{MEDIC}-agent-worker:6990"
    # A3-3: the probe tries the gateway's inbound listener, which only the
    # backend may reach; the control is the outbound listener on the same pods,
    # so "control connects" proves the target has live endpoints (re-review).
    assert env["VIGIL_MEDIC_POLICY_PROBE_ADDR"] == f"{GATEWAY}-in:8470"
    assert env["VIGIL_MEDIC_POLICY_CONTROL_ADDR"] == f"{GATEWAY}:8471"


def test_probe_target_cant_be_overridden(chart) -> None:
    # Any other target can only weaken the proof: a dead address always times
    # out, which would read as "enforced" (S7-8).
    with pytest.raises(RenderError, match=r"medic\.policyProbe"):
        helm_template(chart, {**MEDIC_ON, "medic.policyProbe.target": "10.9.9.9:1"})


def test_no_service_links(on) -> None:
    # Service env vars would hand Medic every Service address in the namespace.
    assert medic_pod(on)["enableServiceLinks"] is False
    assert gateway_pod(on)["enableServiceLinks"] is False


# --- C3 §4.5: identity and RBAC (check 2) ------------------------------------


def test_medic_sa_mounts_its_token_gateway_sa_doesnt(on) -> None:
    assert named(on, "ServiceAccount", MEDIC)["automountServiceAccountToken"] is True
    assert named(on, "ServiceAccount", GATEWAY)["automountServiceAccountToken"] is False
    assert medic_pod(on)["serviceAccountName"] == MEDIC
    gw = gateway_pod(on)
    assert gw["serviceAccountName"] == GATEWAY
    assert gw["automountServiceAccountToken"] is False


def test_role_is_exactly_c3s_reads(on) -> None:
    role = named(on, "Role", MEDIC)
    assert role["metadata"]["namespace"] == NAMESPACE
    rules = {
        (tuple(r["apiGroups"]), tuple(r["resources"]), tuple(r["verbs"]))
        for r in role["rules"]
    }
    assert rules == {
        (("",), ("pods",), ("get", "list", "watch")),
        # SP2 ⚑5 (a): follow is a get; list/watch don't exist on the subresource.
        (("",), ("pods/log",), ("get",)),
        (("",), ("events",), ("get", "list", "watch")),
        (("apps",), ("deployments", "statefulsets"), ("get", "list", "watch")),
    }
    flat = str(role["rules"])
    for banned in (
        "secrets",
        "configmaps",
        "pods/exec",
        "pods/attach",
        "pods/portforward",
        "nodes",
        "'*'",
        "create",
        "update",
        "patch",
        "delete",
    ):
        assert banned not in flat


def test_rolebinding_binds_only_medics_sa(on) -> None:
    rb = named(on, "RoleBinding", MEDIC)
    assert rb["roleRef"] == {
        "apiGroup": "rbac.authorization.k8s.io",
        "kind": "Role",
        "name": MEDIC,
    }
    assert rb["subjects"] == [
        {"kind": "ServiceAccount", "name": MEDIC, "namespace": NAMESPACE}
    ]


def test_nothing_cluster_scoped(all_on) -> None:
    assert not [d for d in all_on if d["kind"] in ("ClusterRole", "ClusterRoleBinding")]


# --- R6: secrets by reference only (check 2) ---------------------------------


def test_no_inline_value_for_any_secret_name_in_any_pod(all_on) -> None:
    inline = []
    for d in all_on:
        if d["kind"] not in WORKLOADS:
            continue
        for c in containers(pod_spec(d)):
            for e in c.get("env", []):
                if e["name"] in SECRET_NAMES and "value" in e:
                    inline.append(f"{d['metadata']['name']}/{c['name']}: {e['name']}")
    assert inline == []


def test_medic_and_gateway_hold_no_agent_token_and_no_envfrom(on) -> None:
    for spec in (medic_pod(on), gateway_pod(on)):
        for c in containers(spec):
            assert "envFrom" not in c
            names = {e["name"] for e in c.get("env", [])}
            assert not names & AGENT_TOKENS
            assert not names & SECRET_NAMES


def test_medic_holds_no_viewer_password(on) -> None:
    spec = medic_pod(on)
    (c,) = spec["containers"]
    assert not [e for e in c["env"] if "PASSWORD" in e["name"] or "valueFrom" in e]
    # Its one secret is the API key (V2-8), never the Viewer's.
    secrets = [v["secret"]["secretName"] for v in spec["volumes"] if "secret" in v]
    assert secrets == [f"{MEDIC}-api-key"]


def test_gateway_reads_the_viewer_password_from_one_secret_key(on) -> None:
    spec = gateway_pod(on)
    (vol,) = [v for v in spec["volumes"] if "secret" in v]
    assert vol["secret"]["secretName"] == "medic-viewer"
    assert vol["secret"]["items"] == [
        {"key": "password", "path": "medic_viewer_password"}
    ]
    (c,) = spec["containers"]
    env = {e["name"]: e.get("value") for e in c["env"]}
    assert (
        env["VIGIL_MEDIC_GATEWAY_VIEWER_PASSWORD_FILE"]
        == "/run/secrets/medic_viewer_password"
    )
    assert "VIGIL_MEDIC_GATEWAY_VIEWER_PASSWORD" not in env
    assert env["VIGIL_MEDIC_GATEWAY_VIEWER_USER"] == "medic-viewer"


@pytest.mark.parametrize(
    "missing",
    ["medic.gateway.viewer.username", "medic.gateway.viewer.passwordSecret.name"],
)
def test_viewer_values_are_required(chart, missing: str) -> None:
    values = {k: v for k, v in MEDIC_ON.items() if k != missing}
    with pytest.raises(RenderError, match=missing.replace(".", r"\.")):
        helm_template(chart, values)


# --- the gateway (S5 contract) ------------------------------------------------


def test_gateway_is_its_own_deployment(on) -> None:
    dep = named(on, "Deployment", GATEWAY)
    spec = pod_spec(dep)
    (c,) = spec["containers"]
    assert c["name"] == "medic-gateway"
    sc = spec["securityContext"]
    assert sc["runAsUser"] == sc["runAsGroup"] == 10002
    assert c["securityContext"]["readOnlyRootFilesystem"] is True
    env = {e["name"]: e.get("value") for e in c["env"]}
    assert env["VIGIL_MEDIC_GATEWAY_BACKEND"] == "rel-vigil-backend:6987"
    assert env["VIGIL_MEDIC_GATEWAY_MEDIC"] == f"{MEDIC}:8470"
    # One pod IP, two listeners: the policy, not the bind, splits them on Helm.
    assert env["VIGIL_MEDIC_GATEWAY_OUT_BIND"] == "$(POD_IP):8471"
    assert env["VIGIL_MEDIC_GATEWAY_IN_BIND"] == "$(POD_IP):8470"
    for probe in ("livenessProbe", "readinessProbe"):
        assert c[probe]["exec"]["command"] == [
            "python",
            "-m",
            "services.medic_gateway",
            "check",
        ]


def test_one_service_per_gateway_listener(on) -> None:
    out = named(on, "Service", GATEWAY)
    inb = named(on, "Service", f"{GATEWAY}-in")
    assert [p["port"] for p in out["spec"]["ports"]] == [8471]
    assert [p["port"] for p in inb["spec"]["ports"]] == [8470]


# --- C3 §4.4: labels and NetworkPolicy (check 2) -----------------------------


def test_own_labels_never_the_charts_selector_pair(on) -> None:
    for name in (MEDIC, GATEWAY):
        labels = pod_labels(named(on, "Deployment", name))
        assert labels.get("app.kubernetes.io/name") != "vigil", name


def test_no_policy_but_their_own_selects_medic_or_gateway(all_on) -> None:
    pods = {n: pod_labels(named(all_on, "Deployment", n)) for n in (MEDIC, GATEWAY)}
    for pol in kinds(all_on, "NetworkPolicy"):
        name = pol["metadata"]["name"]
        for pod, labels in pods.items():
            if selects(pol["spec"]["podSelector"], labels):
                assert name in OWN_POLICIES, f"{name} selects {pod}"


def test_the_trap_this_guards_is_real(all_on) -> None:
    # The catch-all allows all egress and selects every pod with the chart's pair.
    catch_all = named(all_on, "NetworkPolicy", "rel-vigil-deny-all")
    assert catch_all["spec"]["egress"] == [{}]
    backend = pod_labels(named(all_on, "Deployment", "rel-vigil-backend"))
    assert selects(catch_all["spec"]["podSelector"], backend)


@pytest.mark.parametrize("chart_policies", ["true", "false"])
def test_medic_policies_render_whatever_the_chart_switch(
    render, chart_policies
) -> None:
    docs = render({**MEDIC_ON, "networkPolicies.enabled": chart_policies})
    names = {d["metadata"]["name"] for d in kinds(docs, "NetworkPolicy")}
    assert OWN_POLICIES <= names


def test_deny_all_covers_both_pods_both_ways(on) -> None:
    pol = named(on, "NetworkPolicy", f"{MEDIC}-deny-all")
    assert sorted(pol["spec"]["policyTypes"]) == ["Egress", "Ingress"]
    assert "ingress" not in pol["spec"] and "egress" not in pol["spec"]
    for name in (MEDIC, GATEWAY):
        assert selects(
            pol["spec"]["podSelector"], pod_labels(named(on, "Deployment", name))
        )


def _peers(rule):
    return rule.get("to", rule.get("from", []))


def _allowed_pods(rules, docs):
    """Which rendered Deployments/StatefulSets the rules admit (in this namespace)."""
    out = set()
    for d in docs:
        if d["kind"] not in ("Deployment", "StatefulSet"):
            continue
        for r in rules:
            for peer in _peers(r):
                if (
                    "podSelector" in peer
                    and "namespaceSelector" not in peer
                    and selects(peer["podSelector"], pod_labels(d))
                ):
                    out.add(d["metadata"]["name"])
    return out


def test_medic_egress_is_gateway_agent_worker_dns_and_kube_api(all_on) -> None:
    pol = named(all_on, "NetworkPolicy", MEDIC)
    assert sorted(pol["spec"]["policyTypes"]) == ["Egress", "Ingress"]
    egress = pol["spec"]["egress"]
    assert _allowed_pods(egress, all_on) == {GATEWAY, "rel-vigil-agent-worker"}
    blocks = [p["ipBlock"] for r in egress for p in _peers(r) if "ipBlock" in p]
    assert blocks == [{"cidr": KUBE_API}]
    api_rule = next(r for r in egress if any("ipBlock" in p for p in _peers(r)))
    assert sorted(p["port"] for p in api_rule["ports"]) == [443, 6443]
    for r in egress:
        assert r.get("to"), "an egress rule with no `to` allows every destination"
        assert r.get("ports"), "an egress rule with no ports allows every port"


def test_probe_target_is_one_medics_own_policy_blocks(on) -> None:
    # The target (gateway inbound 8470) must not be in Medic's egress, the
    # control (gateway outbound 8471) must be, and they share pods.
    egress = named(on, "NetworkPolicy", MEDIC)["spec"]["egress"]
    gw_ports = [
        p["port"]
        for r in egress
        if _allowed_pods([r], on) == {GATEWAY}
        for p in r["ports"]
    ]
    assert gw_ports == [8471]
    assert (
        named(on, "Service", f"{GATEWAY}-in")["spec"]["selector"]
        == named(on, "Service", GATEWAY)["spec"]["selector"]
    )


def test_medic_egress_never_allows_the_backend_or_bifrost(all_on) -> None:
    egress = named(all_on, "NetworkPolicy", MEDIC)["spec"]["egress"]
    allowed = _allowed_pods(egress, all_on)
    for forbidden in ("rel-vigil-backend", "rel-vigil-redis", "rel-vigil-postgres"):
        assert forbidden not in allowed
    flat = str(egress)
    assert "0.0.0.0/0" not in flat and "::/0" not in flat
    assert "bifrost" not in flat.lower()


def test_medic_ingress_only_from_the_gateway(on) -> None:
    ingress = named(on, "NetworkPolicy", MEDIC)["spec"]["ingress"]
    assert _allowed_pods(ingress, on) == {GATEWAY}
    assert [p["port"] for r in ingress for p in r["ports"]] == [8470]


def test_gateway_policy(all_on) -> None:
    pol = named(all_on, "NetworkPolicy", GATEWAY)["spec"]
    by_port = {}
    for r in pol["ingress"]:
        for p in r["ports"]:
            by_port.setdefault(p["port"], set()).update(_allowed_pods([r], all_on))
    # Medic → outbound listener; backend → inbound listener (and nothing crosses).
    assert by_port == {8471: {MEDIC}, 8470: {"rel-vigil-backend"}}
    egress_pods = _allowed_pods(pol["egress"], all_on)
    assert egress_pods == {"rel-vigil-backend", MEDIC}
    assert "ipBlock" not in str(pol["egress"])


def test_dns_egress_is_kube_dns_only(on) -> None:
    for name in (MEDIC, GATEWAY):
        egress = named(on, "NetworkPolicy", name)["spec"]["egress"]
        dns = [r for r in egress if any("namespaceSelector" in p for p in _peers(r))]
        assert len(dns) == 1
        (peer,) = dns[0]["to"]
        assert peer["namespaceSelector"] == {
            "matchLabels": {"kubernetes.io/metadata.name": "kube-system"}
        }
        assert peer["podSelector"] == {"matchLabels": {"k8s-app": "kube-dns"}}
        assert sorted((p["protocol"], p["port"]) for p in dns[0]["ports"]) == [
            ("TCP", 53),
            ("UDP", 53),
        ]


def test_agent_worker_service_selects_the_agent_worker(on) -> None:
    svc = named(on, "Service", f"{MEDIC}-agent-worker")
    assert [p["port"] for p in svc["spec"]["ports"]] == [6990]
    assert selects(
        {"matchLabels": svc["spec"]["selector"]},
        pod_labels(named(on, "Deployment", "rel-vigil-agent-worker")),
    )


# --- check 15: the Kubernetes API address -----------------------------------


def test_template_without_lookup_fails_unless_cidrs_set(chart) -> None:
    values = {k: v for k, v in MEDIC_ON.items() if not k.startswith("medic.kubeApi")}
    with pytest.raises(RenderError, match=r"medic\.kubeApi\.cidrs"):
        helm_template(chart, values)


def test_cidrs_must_look_like_cidrs(chart) -> None:
    with pytest.raises(RenderError, match=r"medic\.kubeApi\.cidrs"):
        helm_template(chart, {**MEDIC_ON, "medic.kubeApi.cidrs[0]": "0.0.0.0/0"})


# --- review fixes (S7 review #1-#4, name lengths) ---------------------------


def test_probe_has_a_positive_control_the_policy_allows(on) -> None:
    (c,) = medic_pod(on)["containers"]
    env = {e["name"]: e.get("value") for e in c["env"]}
    assert env["VIGIL_MEDIC_POLICY_CONTROL_ADDR"] == f"{GATEWAY}:8471"


@pytest.mark.parametrize(
    "cidrs",
    [["0.0.0.0/1", "128.0.0.0/1"], ["10.0.0.0/16"], ["::/1"], ["fd00::/64"]],
)
def test_kube_api_cidrs_have_a_prefix_floor(chart, cidrs) -> None:
    values = {k: v for k, v in MEDIC_ON.items() if not k.startswith("medic.kubeApi")}
    for i, cidr in enumerate(cidrs):
        values[f"medic.kubeApi.cidrs[{i}]"] = cidr
    with pytest.raises(RenderError, match=r"medic\.kubeApi\.cidrs"):
        helm_template(chart, values)


def test_kube_api_cidrs_accept_a_small_range(render) -> None:
    render({**MEDIC_ON, "medic.kubeApi.cidrs[0]": "10.0.0.0/28"})


@pytest.mark.parametrize(
    "key, value, error",
    [
        ("medic.kubeApi.ports", "null", r"medic\.kubeApi\.ports"),
        ("medic.dns.podLabels", "null", r"medic\.dns\.podLabels"),
        ("agentWorker.enabled", "false", r"agentWorker\.enabled"),
        ("medic.kubeApi.ports[0]", "99999", r"medic\.kubeApi\.ports"),
    ],
)
def test_values_that_would_widen_or_break_a_policy_fail(
    chart, key, value, error
) -> None:
    with pytest.raises(RenderError, match=error):
        helm_template(chart, {**MEDIC_ON, key: value})


def test_long_release_names_fit_63(chart) -> None:
    long = "r" * 53  # helm's own release-name limit
    args = ["helm", "template", long, str(chart), "--namespace", NAMESPACE]
    for key, value in MEDIC_ON.items():
        args += ["--set", f"{key}={value}"]
    proc = subprocess.run(args, capture_output=True, text=True, check=True)
    docs = [d for d in yaml.safe_load_all(proc.stdout) if d]
    names = [d["metadata"]["name"] for d in docs if "medic" in str(d["metadata"])]
    assert names and all(len(n) <= 63 for n in names), [n for n in names if len(n) > 63]
    pairs = {
        (d["kind"], d["metadata"]["name"])
        for d in docs
        if "medic" in d["metadata"]["name"]
    }
    assert len(pairs) == 15  # nothing collided when truncated


def test_blank_cidrs_mean_look_it_up(chart) -> None:
    # `--set medic.kubeApi.cidrs={}` renders [""]: that's "unset", so lookup
    # runs, and under `helm template` (no lookup) the value is required.
    values = {k: v for k, v in MEDIC_ON.items() if not k.startswith("medic.kubeApi")}
    with pytest.raises(RenderError, match=r"medic\.kubeApi\.cidrs is required"):
        helm_template(chart, {**values, "medic.kubeApi.cidrs": "{}"})


# --- V2-8: the API key, and the backend's poll -------------------------------

import base64
import re

API_KEY = f"{MEDIC}-api-key"
KEY_DIR = "/run/secrets/medic-api"
BACKEND = "rel-vigil-backend"


def backend_pod(docs):
    return pod_spec(named(docs, "Deployment", BACKEND))


def secret_volume(spec, secret_name):
    vols = [v for v in spec.get("volumes", []) if "secret" in v]
    return [v for v in vols if v["secret"]["secretName"] == secret_name]


def test_api_key_is_generated_into_its_own_secret(on) -> None:
    doc = named(on, "Secret", API_KEY)
    assert doc["type"] == "Opaque"
    assert set(doc["data"]) == {"api_key"}
    key = base64.b64decode(doc["data"]["api_key"]).decode()
    # X2's shape: 32 random bytes, base64url, no padding (the gateway and Medic
    # refuse anything else).
    assert re.fullmatch(r"[A-Za-z0-9_-]{43}", key), key
    assert "stringData" not in doc


def test_api_key_is_never_in_values(chart) -> None:
    values = yaml.safe_load((chart / "values.yaml").read_text())["medic"]["apiKey"]
    assert values == {"existingSecret": "", "key": "api_key"}


def test_two_installs_get_different_keys(render, chart) -> None:
    a = named(helm_template(chart, MEDIC_ON), "Secret", API_KEY)["data"]["api_key"]
    b = named(helm_template(chart, MEDIC_ON), "Secret", API_KEY)["data"]["api_key"]
    assert a != b


def test_api_key_value_appears_only_in_its_secret(all_on) -> None:
    key = base64.b64decode(named(all_on, "Secret", API_KEY)["data"]["api_key"]).decode()
    elsewhere = [
        f"{d['kind']}/{d['metadata']['name']}"
        for d in all_on
        if key in yaml.safe_dump(d) and d["metadata"]["name"] != API_KEY
    ]
    assert elsewhere == []


@pytest.mark.parametrize("pod", [medic_pod, backend_pod])
def test_medic_and_backend_mount_the_key_as_a_file(on, pod) -> None:
    spec = pod(on)
    (vol,) = secret_volume(spec, API_KEY)
    # Kubernetes gives a secret file the pod's fsGroup (Medic 10001, backend
    # 1000), so group-read is enough and "other" gets nothing (S6-3 / V2-7).
    assert vol["secret"]["defaultMode"] == 0o440
    assert vol["secret"]["items"] == [{"key": "api_key", "path": "api_key"}]
    (c,) = spec["containers"]
    mounts = [m for m in c["volumeMounts"] if m["name"] == vol["name"]]
    assert mounts == [{"name": vol["name"], "mountPath": KEY_DIR, "readOnly": True}]
    env = {e["name"]: e for e in c["env"]}
    assert env["VIGIL_MEDIC_API_KEY_FILE"] == {
        "name": "VIGIL_MEDIC_API_KEY_FILE",
        "value": f"{KEY_DIR}/api_key",
    }
    assert "envFrom" not in c or all(
        API_KEY not in str(src) for src in c.get("envFrom", [])
    )
    assert not [e for e in c["env"] if API_KEY in str(e.get("valueFrom", ""))]


def test_backend_polls_through_the_gateways_inbound_listener(on) -> None:
    (c,) = backend_pod(on)["containers"]
    env = {e["name"]: e.get("value") for e in c["env"]}
    assert env["VIGIL_MEDIC_API_URL"] == f"http://{GATEWAY}-in:8470"
    assert env["VIGIL_MEDIC_ENABLED"] == "true"


def test_gateway_never_holds_the_api_key(on) -> None:
    # It passes X-Medic-Key through and checks only its shape; Medic compares it.
    assert secret_volume(gateway_pod(on), API_KEY) == []
    (c,) = gateway_pod(on)["containers"]
    assert not [e for e in c["env"] if "API_KEY" in e["name"]]


def test_medic_off_gives_the_backend_no_medic_path(render) -> None:
    docs = render()
    spec = backend_pod(docs)
    (c,) = spec["containers"]
    names = {e["name"] for e in c["env"]}
    assert not names & {"VIGIL_MEDIC_API_URL", "VIGIL_MEDIC_API_KEY_FILE"}
    assert not [v for v in spec.get("volumes", []) if "medic" in v["name"]]
    assert [
        d for d in docs if d["kind"] == "Secret" and "medic" in d["metadata"]["name"]
    ] == []


def test_existing_secret_replaces_the_generated_one(render) -> None:
    docs = render(
        {
            **MEDIC_ON,
            "medic.apiKey.existingSecret": "ops-medic",
            "medic.apiKey.key": "k",
        }
    )
    assert [d for d in kinds(docs, "Secret") if d["metadata"]["name"] == API_KEY] == []
    for spec in (medic_pod(docs), backend_pod(docs)):
        (vol,) = secret_volume(spec, "ops-medic")
        assert vol["secret"]["items"] == [{"key": "k", "path": "api_key"}]


def test_backend_and_medic_mount_the_same_secret(on) -> None:
    (m,) = secret_volume(medic_pod(on), API_KEY)
    (b,) = secret_volume(backend_pod(on), API_KEY)
    assert m["secret"] == b["secret"]


def test_gateway_admits_the_backend_on_the_inbound_port(all_on) -> None:
    policy = named(all_on, "NetworkPolicy", GATEWAY)
    backend_labels = pod_labels(named(all_on, "Deployment", BACKEND))
    admitted = [
        p["port"]
        for rule in policy["spec"]["ingress"]
        for peer in rule["from"]
        if selects(peer.get("podSelector", {}), backend_labels)
        for p in rule["ports"]
    ]
    assert admitted == [8470]
