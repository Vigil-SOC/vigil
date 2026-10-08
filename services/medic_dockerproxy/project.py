"""Project inspect and list replies to a field allowlist (SP2 §3, ⚑1 (a)).

Deny by default: a field not listed here never leaves the proxy, so `Config.Env`,
`Cmd`, `Args`, `Entrypoint`, `Path`, `Health.Log` and whatever Docker adds next
are dropped without being named. Each kept field has a type; a reply that
doesn't fit (wrong type, wrong shape) raises Unprojectable and the proxy answers
502 rather than guess. `null` is kept as `null` (Docker sends it for empty maps).
"""

from __future__ import annotations

LABELS = "labels"  # {str: str}

INSPECT = {
    "Id": str,
    "Name": str,
    "Created": str,
    "RestartCount": int,
    "Image": str,
    "State": {
        "Status": str,
        "Running": bool,
        "Paused": bool,
        "Restarting": bool,
        "OOMKilled": bool,
        "Dead": bool,
        "ExitCode": int,
        "StartedAt": str,
        "FinishedAt": str,
        "Health": {"Status": str, "FailingStreak": int},
    },
    "Config": {"Image": str, "Labels": LABELS, "Tty": bool},
    "HostConfig": {
        "LogConfig": {"Type": str},
        "RestartPolicy": {"Name": str, "MaximumRetryCount": int},
        "Memory": int,
        "NanoCpus": int,
    },
}
LIST = [
    {
        "Id": str,
        "Names": [str],
        "Image": str,
        "Labels": LABELS,
        "State": str,
        "Status": str,
        "Created": int,
    }
]


class Unprojectable(Exception):
    pass


def project(value, spec):
    if value is None:
        return None
    if isinstance(spec, dict):
        if not isinstance(value, dict):
            raise Unprojectable
        return {k: project(value[k], s) for k, s in spec.items() if k in value}
    if isinstance(spec, list):
        if not isinstance(value, list):
            raise Unprojectable
        return [project(v, spec[0]) for v in value]
    if spec == LABELS:
        if not isinstance(value, dict) or not all(
            isinstance(k, str) and isinstance(v, str) for k, v in value.items()
        ):
            raise Unprojectable
        return value
    if type(value) is not spec:  # exact: a bool is not an int here
        raise Unprojectable
    return value


def inspect(doc):
    if not isinstance(doc, dict):
        raise Unprojectable
    return project(doc, INSPECT)


def listing(doc):
    if not isinstance(doc, list):
        raise Unprojectable
    return project(doc, LIST)
