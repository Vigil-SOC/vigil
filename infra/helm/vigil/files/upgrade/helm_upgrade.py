"""The Helm side of ``python -m core.upgrade``: stop this release, then upgrade.

``apply`` is the weight-0 pre-upgrade hook. It checks the acknowledgement,
records the scale of every Deployment and StatefulSet of this release (not
Postgres or Redis) and the KEDA pause annotation on their ScaledObjects, pauses
KEDA, scales them to 0, waits until their pods are gone, terminates leftover
``vigil_app`` sessions, and runs ``python -m core.upgrade``. On success it
un-pauses KEDA and leaves the scaling to the upgraded manifests Helm applies
next. On failure with ``alembic_version`` unchanged it restores what it
recorded; once a revision has run it leaves everything at 0.

``install`` is the post-install hook: the acknowledgement check and the
upgrade command only. Nothing old is running, so nothing is scaled.

The Kubernetes API is plain HTTPS with the pod's token: no client package.
"""

from __future__ import annotations

import json
import os
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Optional

import psycopg2

SA_DIR = Path("/var/run/secrets/kubernetes.io/serviceaccount")
KEDA_PAUSE = "autoscaling.keda.sh/paused-replicas"
# Left running: the database and the queue the upgrade needs, and Bifrost,
# which the chart does not render but an operator might label as part of it.
KEEP = ("postgres", "redis", "bifrost", "upgrade")
KINDS = (("deployments", "Deployment"), ("statefulsets", "StatefulSet"))
RECORD = "helm-scale.json"


def say(msg: str) -> None:
    print(f"vigil-upgrade: {msg}", flush=True)


class Kube:
    def __init__(self) -> None:
        self.base = os.environ.get("VIGIL_KUBE_API", "https://kubernetes.default.svc")
        sa = Path(os.environ.get("VIGIL_KUBE_SA_DIR", str(SA_DIR)))
        self.token = (sa / "token").read_text().strip()
        self.ns = (
            os.environ.get("VIGIL_NAMESPACE") or (sa / "namespace").read_text().strip()
        )
        self.ctx = (
            ssl.create_default_context(cafile=str(sa / "ca.crt"))
            if self.base.startswith("https:")
            else None
        )

    def call(self, method: str, path: str, body: Any = None) -> Optional[dict]:
        """JSON from the API; None for a 404 (a missing CRD or object)."""
        req = urllib.request.Request(
            self.base + path,
            method=method,
            data=None if body is None else json.dumps(body).encode(),
            headers={
                "Authorization": f"Bearer {self.token}",
                "Accept": "application/json",
                "Content-Type": "application/merge-patch+json",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=30, context=self.ctx) as resp:
                return json.loads(resp.read() or b"{}")
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            raise RuntimeError(
                f"{method} {path}: HTTP {e.code} {e.read()[:300]!r}"
            ) from e

    def items(self, path: str, selector: str = "") -> list[dict]:
        q = f"?labelSelector={urllib.parse.quote(selector)}" if selector else ""
        found = self.call("GET", path + q)
        return [] if found is None else found.get("items", [])

    def apps(self, plural: str) -> str:
        return f"/apis/apps/v1/namespaces/{self.ns}/{plural}"

    def scaledobjects(self) -> str:
        return f"/apis/keda.sh/v1alpha1/namespaces/{self.ns}/scaledobjects"

    def scale(self, plural: str, name: str, replicas: int) -> None:
        self.call(
            "PATCH",
            f"{self.apps(plural)}/{name}/scale",
            {"spec": {"replicas": replicas}},
        )

    def pause(self, name: str, value: Optional[str]) -> None:
        """Set the KEDA pause annotation; None removes it."""
        self.call(
            "PATCH",
            f"{self.scaledobjects()}/{name}",
            {"metadata": {"annotations": {KEDA_PAUSE: value}}},
        )


# --- the record ------------------------------------------------------------


def release_selector() -> str:
    release = os.environ["VIGIL_RELEASE"]
    return (
        f"app.kubernetes.io/instance={release},app.kubernetes.io/part-of=vigil,"
        f"app.kubernetes.io/component notin ({','.join(KEEP)})"
    )


def take_record(kube: Kube) -> dict:
    workloads = []
    for plural, kind in KINDS:
        for obj in kube.items(kube.apps(plural), release_selector()):
            labels = obj["metadata"].get("labels") or {}
            if labels.get("app.kubernetes.io/component") in KEEP:
                continue  # the selector already says so; belt and braces
            name = obj["metadata"]["name"]
            scale = kube.call("GET", f"{kube.apps(plural)}/{name}/scale") or {}
            workloads.append(
                {
                    "plural": plural,
                    "kind": kind,
                    "name": name,
                    "replicas": int((scale.get("spec") or {}).get("replicas") or 0),
                    "selector": obj["spec"]["selector"].get("matchLabels") or {},
                }
            )
    targets = {(w["kind"], w["name"]) for w in workloads}
    keda = []
    # A missing CRD is a 404: nothing to pause.
    for so in kube.items(kube.scaledobjects()):
        ref = so["spec"].get("scaleTargetRef") or {}
        if (ref.get("kind", "Deployment"), ref.get("name")) in targets:
            annotations = so["metadata"].get("annotations") or {}
            keda.append(
                {"name": so["metadata"]["name"], "paused": annotations.get(KEDA_PAUSE)}
            )
    # HPAs are not patched: the API rejects maxReplicas 0, and an HPA stops
    # acting on a target whose replicas are 0 (condition ScalingDisabled).
    return {"workloads": workloads, "keda": keda}


def load_or_take_record(kube: Kube, upgrade_dir: Path) -> dict:
    """A record left by a run that died mid-way wins: the live scale is now 0."""
    path = upgrade_dir / RECORD
    if path.exists():
        say(f"using the scale recorded by an unfinished run ({path})")
        return json.loads(path.read_text())
    record = take_record(kube)
    upgrade_dir.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record, indent=2))
    return record


def forget_record(upgrade_dir: Path) -> None:
    (upgrade_dir / RECORD).unlink(missing_ok=True)


def describe(record: dict) -> str:
    return ", ".join(
        f"{w['kind']}/{w['name']}={w['replicas']}" for w in record["workloads"]
    )


# --- stop and restore ----------------------------------------------------------


def stop(kube: Kube, record: dict, timeout: float) -> None:
    for so in record["keda"]:
        kube.pause(so["name"], "0")
    for w in record["workloads"]:
        kube.scale(w["plural"], w["name"], 0)
    say(f"scaled to 0: {describe(record)}")
    deadline = time.monotonic() + timeout
    while True:
        left = [
            p["metadata"]["name"]
            for w in record["workloads"]
            for p in kube.items(
                f"/api/v1/namespaces/{kube.ns}/pods",
                ",".join(f"{k}={v}" for k, v in sorted(w["selector"].items())),
            )
        ]
        if not left:
            return
        if time.monotonic() >= deadline:
            raise RuntimeError(
                f"pods still running after {timeout:.0f}s: {', '.join(left)}"
            )
        time.sleep(2)


def unpause_keda(kube: Kube, record: dict) -> None:
    for so in record["keda"]:
        kube.pause(so["name"], so["paused"])


def restore(kube: Kube, record: dict) -> None:
    for w in record["workloads"]:
        kube.scale(w["plural"], w["name"], w["replicas"])
    unpause_keda(kube, record)
    say(f"restored: {describe(record)}")


# --- Postgres ------------------------------------------------------------------


def connect():
    return psycopg2.connect(
        host=os.environ["POSTGRES_HOST"],
        port=os.environ.get("POSTGRES_PORT", "5432"),
        dbname=os.environ["POSTGRES_DB"],
        user=os.environ["POSTGRES_USER"],
        password=os.environ["POSTGRES_PASSWORD"],
        sslmode=os.environ.get("POSTGRES_SSL_MODE", "prefer"),
        connect_timeout=5,
    )


def wait_for_db(timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while True:
        try:
            connect().close()
            return
        except psycopg2.OperationalError as e:
            if time.monotonic() >= deadline:
                raise RuntimeError(
                    f"Postgres not reachable after {timeout:.0f}s: {e}"
                ) from e
            time.sleep(2)


def revision() -> Optional[str]:
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT to_regclass('public.alembic_version')")
            if cur.fetchone()[0] is None:
                return None
            cur.execute("SELECT version_num FROM public.alembic_version")
            row = cur.fetchone()
            return row[0] if row else None
    finally:
        conn.close()


def terminate_app_sessions() -> None:
    """End the vigil_app sessions left after the pods are gone."""
    conn = connect()
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT pid FROM pg_stat_activity WHERE usename = 'vigil_app' "
                "AND datname = current_database() AND pid <> pg_backend_pid()"
            )
            pids = [r[0] for r in cur.fetchall()]
            for pid in pids:
                cur.execute("SELECT pg_terminate_backend(%s)", (pid,))
        if pids:
            say(f"terminated {len(pids)} vigil_app session(s)")
    finally:
        conn.close()


def set_app_password() -> None:
    """vigil_app's password, as Compose's 00_apply.sh sets it: never the owner's."""
    app_pw = os.environ.get("POSTGRES_APP_PASSWORD", "")
    if not app_pw:
        raise RuntimeError(
            "POSTGRES_APP_PASSWORD is empty; refusing to give vigil_app the owner password"
        )
    if app_pw == os.environ.get("POSTGRES_PASSWORD"):
        raise RuntimeError(
            "POSTGRES_APP_PASSWORD must differ from the database owner password"
        )
    conn = connect()
    try:
        with conn, conn.cursor() as cur:
            cur.execute("ALTER ROLE vigil_app PASSWORD %s", (app_pw,))
            cur.execute(
                "REVOKE INSERT, UPDATE, DELETE, TRUNCATE ON TABLE public.alembic_version FROM vigil_app"
            )
            cur.execute("GRANT SELECT ON TABLE public.alembic_version TO vigil_app")
    finally:
        conn.close()
    say("set vigil_app password")


def upgrade() -> int:
    """``python -m core.upgrade`` with the acknowledgement, then the app password."""
    code = subprocess.run([sys.executable, "-m", "core.upgrade"]).returncode
    if code != 0:
        return code
    try:
        set_app_password()
    except Exception as e:  # noqa: BLE001 - reported, non-zero exit
        say(f"ERROR: {e}")
        return 1
    return 0


# --- entry points ----------------------------------------------------------------


def ack_error() -> Optional[str]:
    expected = os.environ["VIGIL_CHART_APP_VERSION"]
    version = os.environ.get("VIGIL_UPGRADE_ACK_VERSION", "")
    if version != expected:
        return (
            f"upgrade.acknowledgeVersion is {version or 'empty'!r}, not this release's "
            f"{expected}: set it to {expected} (a value from an earlier upgrade refuses)"
        )
    if not os.environ.get("VIGIL_UPGRADE_ACK_REASON", "").strip():
        return "upgrade.acknowledgeReason is empty: say why there is no Vigil backup"
    return None


def apply() -> int:
    upgrade_dir = Path(os.environ["VIGIL_UPGRADE_DIR"])
    timeout = float(os.environ.get("VIGIL_UPGRADE_STOP_TIMEOUT", "600"))
    wait_for_db(60)
    before = revision()
    kube = Kube()
    record = load_or_take_record(kube, upgrade_dir)
    try:
        stop(kube, record, timeout)
        terminate_app_sessions()
    except Exception as e:  # noqa: BLE001 - restored, then non-zero exit
        say(f"ERROR before the upgrade ran: {e}")
        restore(kube, record)
        forget_record(upgrade_dir)
        return 1

    code = upgrade()
    if code == 0:
        unpause_keda(kube, record)
        forget_record(upgrade_dir)
        say("upgrade done; Helm now applies the new manifests")
        return 0
    try:
        after = revision()
    except Exception as e:  # noqa: BLE001 - unknown counts as changed
        say(f"could not read alembic_version after the failure: {e}")
        after = object()
    forget_record(upgrade_dir)
    if after == before:
        say(f"upgrade exited {code}; alembic_version is still {before}: restoring")
        restore(kube, record)
        return code
    say(
        f"upgrade exited {code} after alembic_version moved from {before}: "
        "workloads stay at 0. Restore your own backup of the database, then run "
        "`helm rollback`. Scales before the upgrade: " + describe(record)
    )
    return code


def install() -> int:
    wait_for_db(float(os.environ.get("VIGIL_UPGRADE_DB_WAIT", "600")))
    return upgrade()


def main(argv: list[str]) -> int:
    if len(argv) != 2 or argv[1] not in ("apply", "install"):
        print("usage: helm_upgrade.py apply|install", file=sys.stderr)
        return 2
    error = ack_error()
    if error:
        say(f"refused: {error}. Nothing was stopped.")
        return 1
    try:
        return apply() if argv[1] == "apply" else install()
    except Exception as e:  # noqa: BLE001 - one line, non-zero exit
        say(f"ERROR: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
