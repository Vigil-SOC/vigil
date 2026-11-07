"""The Helm release the Helm fault cells break (e2e.sh full on kind, with Calico or kindnet).

The real backend, Postgres, Redis, Bifrost, SOC daemon, LLM worker, agent-serve
and agent worker, with Medic and its gateway, and auth on. HelmStack offers the
methods Stack does (`py`, `sql`, `logs`, `push`, `api`, `node`, `wait`,
`set_integration`, `wait_daemon`, ...), done with `kubectl exec` into the pods,
so the verify functions in config_cells.py run on either. Only what Compose does
with `env`/`up`/`recreate` is Helm-specific: `recreate` and `set_env` here.
Service names are Compose's (`soc-daemon`, `siem-stub`, ...), mapped to workloads.
"""

from __future__ import annotations

import json
import secrets
import subprocess
import time

from tests.medic_compose.compose import REPO
from tests.medic_eval.inject.stack import NODE_FETCH, ComposeFailed, Stack

E2E = REPO / "infra" / "helm" / "vigil" / "medic-tests" / "kind" / "e2e.sh"
STUB = REPO / "tests" / "medic_compose" / "stub.py"
# The Elastic the daemon polls (I-4.b): tests/medic_compose/stub.py in a pod, as
# Compose's siem-stub is. Runs in the backend image (Python, already on the node).
STUB_YAML = """\
apiVersion: apps/v1
kind: Deployment
metadata: {{name: siem-stub}}
spec:
  replicas: 1
  selector: {{matchLabels: {{app: siem-stub}}}}
  template:
    metadata: {{labels: {{app: siem-stub}}}}
    spec:
      containers:
        - name: stub
          image: vigil-backend:{tag}
          imagePullPolicy: Never
          command: [python, /stub/stub.py]
          env: [{{name: STUB_ROLE, value: elastic}}]
          ports: [{{containerPort: 9200}}]
          volumeMounts: [{{name: stub, mountPath: /stub}}]
      volumes: [{{name: stub, configMap: {{name: siem-stub}}}}]
---
apiVersion: v1
kind: Service
metadata: {{name: siem-stub}}
spec:
  selector: {{app: siem-stub}}
  ports: [{{port: 9200}}]
"""


_SUFFIX = {"m": 1e-3, "k": 1e3, "M": 1e6, "G": 1e9, "T": 1e12, "Ki": 2**10, "Mi": 2**20, "Gi": 2**30, "Ti": 2**40}


def quantity(text: str) -> float:
    """A Kubernetes quantity ("1000m", "1", "1Gi", "1073741824") as a number."""
    for suffix in sorted(_SUFFIX, key=len, reverse=True):
        if text.endswith(suffix):
            return float(text[: -len(suffix)]) * _SUFFIX[suffix]
    return float(text)


class KubectlFailed(ComposeFailed):
    pass


class HelmStack(Stack):
    medic_key_file = "/run/secrets/medic-api/api_key"  # the chart's apiKeyDir and key path
    medic_services = ("medic", "medic-gateway")  # container names; there is no docker proxy on Helm

    def __init__(self, cluster: str, namespace: str = "vigil", release: str = "rel") -> None:
        # Not Stack.__init__: no Compose project here. The inherited reads and API
        # calls need only these.
        self.cluster = cluster
        self.context = f"kind-{cluster}"
        self.ns = namespace
        fn = f"{release}-vigil"
        self.backend_host = f"{fn}-backend"
        self.hosts = {"backend": self.backend_host, "soc-daemon": f"{fn}-daemon"}
        self.medic_workload = f"deploy/{fn}-medic"
        self.workloads = {
            "backend": f"deploy/{fn}-backend",
            "soc-daemon": f"sts/{fn}-daemon",
            "agent-serve": f"deploy/{fn}-agent-serve",
            "agent-worker": f"deploy/{fn}-agent-worker",
            "postgres": f"sts/{fn}-postgres",
            "siem-stub": "deploy/siem-stub",
        }
        self.admin_password = "Ev" + secrets.token_urlsafe(24) + "!9"
        self.csrf = secrets.token_urlsafe(32)
        self.bearer = ""
        self.good_webhook_token = "EVALHOOK" + secrets.token_hex(12)
        self.agent_token = ""  # the chart Secret's, read at start
        self.pusher_token = self.good_webhook_token
        self.active: set[str] = set()
        self.marks: dict[str, int] = {}

    # ---- kubectl ------------------------------------------------------------

    def kubectl(self, *args: str, check: bool = True, **kw) -> subprocess.CompletedProcess:
        done = subprocess.run(
            ["kubectl", "--context", self.context, "-n", self.ns, *args],
            capture_output=True, text=True, **kw,
        )
        if check and done.returncode:
            raise KubectlFailed(f"kubectl {' '.join(args)[:200]}: {done.stderr[-1500:]}")
        return done

    def rollout(self, *targets: str) -> None:
        for t in targets:
            self.kubectl("rollout", "status", t, "--timeout=300s")

    def recreate(self, *services: str) -> None:
        """What `--force-recreate` is on Compose: a rollout, with the new pod Ready."""
        targets = [self.workloads[s] for s in services]
        for t in targets:
            self.kubectl("rollout", "restart", t)
        self.rollout(*targets)

    def set_env(self, services: tuple[str, ...], name: str, value: str | None) -> None:
        """Set a container env var, or remove it (None) so envFrom's value shows again. No-op if unchanged."""
        arg = f"{name}={value}" if value is not None else f"{name}-"
        for s in services:
            self.kubectl("set", "env", self.workloads[s], arg)
        self.rollout(*(self.workloads[s] for s in services))

    # ---- Stack's reads, through the pods ---------------------------------------

    def py(self, service: str, code: str, *argv: str) -> str:
        return self.kubectl("exec", self.workloads[service], "--", "python", "-c", code, *argv).stdout.strip()

    def sql(self, query: str) -> str:
        return self.kubectl(
            "exec", self.workloads["postgres"], "--",
            "psql", "-U", "deeptempo", "-d", "deeptempo_soc", "-tAc", query,
        ).stdout.strip()

    def http(self, method, host, port, path, *a, **kw):
        return super().http(method, self.hosts.get(host, host), port, path, *a, **kw)

    def node(self, service: str, *argv: str) -> int:
        out = self.kubectl("exec", self.workloads[service], "--", "node", "-e", NODE_FETCH, *argv)
        return int(out.stdout.strip())

    def logs(self, service: str) -> str:
        done = self.kubectl("logs", self.workloads[service], check=False)
        return done.stdout + done.stderr

    # ---- what nightly.py reads from outside Medic ------------------------------------

    def medic_exec(self, *argv: str) -> str:
        return self.kubectl("exec", self.medic_workload, "--", *argv, check=False).stdout

    def _crictl(self) -> dict[str, tuple[int, int]]:
        """{container: (cumulative CPU ns, working-set bytes)} of Medic's containers, from the node."""
        done = subprocess.run(
            ["docker", "exec", f"{self.cluster}-control-plane", "crictl", "stats", "-o", "json"],
            capture_output=True, text=True, check=False,
        )
        rows = {}
        for st in json.loads(done.stdout or '{"stats": []}')["stats"]:
            name = st["attributes"]["metadata"]["name"]
            if name in self.medic_services and st["attributes"]["labels"].get("io.kubernetes.pod.namespace") == self.ns:
                rows[name] = (
                    int(st["cpu"]["usageCoreNanoSeconds"]["value"]),
                    int(st["memory"]["workingSetBytes"]["value"]),
                )
        return rows

    def usage(self) -> dict[str, tuple[float, float]]:
        """{container: (vCPU, MiB)}: two `crictl stats` a couple of seconds apart (no metrics-server on kind)."""
        first, t0 = self._crictl(), time.monotonic()
        time.sleep(2)
        second, dt = self._crictl(), time.monotonic() - t0
        return {
            n: (max(c1 - first[n][0], 0) / 1e9 / dt, mem / 1024**2)
            for n, (c1, mem) in second.items() if n in first
        }

    def medic_limits(self) -> dict:
        """The medic container's limits (None: unset), restarts and OOMKilled, from its pod."""
        sel = json.loads(self.kubectl("get", self.medic_workload, "-o", "jsonpath={.spec.selector.matchLabels}").stdout)
        pods = json.loads(self.kubectl(
            "get", "pods", "-o", "json", "-l", ",".join(f"{k}={v}" for k, v in sel.items()),
        ).stdout)["items"]
        pod = max(pods, key=lambda p: p["metadata"]["creationTimestamp"])  # the newest, if a rollout overlaps
        limits = next(c for c in pod["spec"]["containers"] if c["name"] == "medic").get("resources", {}).get("limits", {})
        status = next(c for c in pod["status"].get("containerStatuses", []) if c["name"] == "medic")
        reasons = [(status.get(k) or {}).get("terminated", {}).get("reason") for k in ("state", "lastState")]
        return {
            "cpu_vcpu": quantity(limits["cpu"]) if "cpu" in limits else None,
            "memory_mib": quantity(limits["memory"]) / 1024**2 if "memory" in limits else None,
            "restarts": status["restartCount"],
            "oom_killed": "OOMKilled" in reasons,
        }

    def not_running(self) -> list[str]:
        """Workloads whose ready replicas are fewer than the spec asks for."""
        bad = []
        for name, w in self.workloads.items():
            got = self.kubectl("get", w, "-o", "jsonpath={.spec.replicas}/{.status.readyReplicas}", check=False).stdout
            want, _, ready = got.partition("/")
            if not want or int(ready or 0) < int(want):
                bad.append(name)
        return bad

    # ---- the faults Compose makes with its environment ---------------------------

    def set_webhook_token(self, token: str) -> None:
        """The daemon reads DAEMON_WEBHOOK_TOKEN once, at start; the chart has no Secret key for it."""
        self.set_env(("soc-daemon",), "DAEMON_WEBHOOK_TOKEN", token)
        self.wait_daemon()

    def set_agent_token(self, token: str | None) -> None:
        """Blank (""), or removed (None) to fall back to the chart Secret's, on agent-serve and agent-worker only."""
        self.set_env(("agent-serve", "agent-worker"), "AGENT_INTERNAL_TOKEN", token)

    # ---- bring-up ---------------------------------------------------------------

    def start(self) -> None:
        """On a cluster e2e.sh full has left up: the stub, the daemon's token, the first admin."""
        self.agent_token = self.kubectl(
            "exec", self.workloads["backend"], "--", "printenv", "AGENT_INTERNAL_TOKEN"
        ).stdout.strip()
        assert self.agent_token, "the chart has no AGENT_INTERNAL_TOKEN"
        self.wait(lambda: self.http("GET", "backend", 6987, "/api/health")[0], lambda c: c == 200)
        cm = self.kubectl(
            "create", "configmap", "siem-stub", f"--from-file=stub.py={STUB}",
            "--dry-run=client", "-o", "yaml",
        ).stdout
        self.kubectl("apply", "-f", "-", input=cm)  # apply: a second start is fine
        tag = self.kubectl("get", self.workloads["backend"], "-o", "jsonpath={..image}").stdout.split(":")[-1]
        self.kubectl("apply", "-f", "-", input=STUB_YAML.format(tag=tag))
        self.rollout(self.workloads["siem-stub"])
        self.set_webhook_token(self.good_webhook_token)
        self.bootstrap_admin()

    def stop(self) -> None:
        subprocess.run(["kind", "delete", "cluster", "--name", self.cluster], capture_output=True, check=False)
