"""`check --ready`: can Medic's API serve? (C5 §5.2, the Helm readiness probe.)

Liveness is the heartbeat (`check`); readiness is only whether the listener
answers the status op, asked the way the gateway asks: Medic's own key file, on
the address the listener binds. Not Ready takes Medic out of its Service, so
the backend's poll reads `refused` rather than reaching a listener that can't
answer. The key is sent, never printed.
"""

from __future__ import annotations

import http.client
from collections.abc import Mapping
from pathlib import Path

from services.medic.api.server import STATUS_PATH, local_address_toward, read_key
from services.medic.app import config

TIMEOUT_S = 5.0  # inside the probe's 10 s timeoutSeconds, with Python's start-up


def check_ready(
    env: Mapping[str, str], data_dir: Path, *, timeout: float = TIMEOUT_S
) -> tuple[bool, str]:
    """Return (ready, reason)."""
    try:
        host, port = config.api_bind(env)
        key_file = config.api_key_file(env, data_dir)
        peer = config.api_bind_peer(env)
    except config.ConfigError as exc:
        return False, f"not ready: {exc}"
    if key_file is None:
        return (
            False,
            f"not ready: {config.API_KEY_FILE_VAR} isn't set, so the API is off",
        )
    key = read_key(key_file)
    if key is None:
        return False, f"not ready: no usable key at {key_file}, so the API is off"
    try:
        if peer is not None:
            host = local_address_toward(peer)
        elif host == "0.0.0.0":
            host = "127.0.0.1"
        conn = http.client.HTTPConnection(host, port, timeout=timeout)
        try:
            conn.request("GET", STATUS_PATH, headers={"X-Medic-Key": key.decode()})
            status = conn.getresponse().status
        finally:
            conn.close()
    # UnicodeError (a ValueError): a peer name the IDNA codec can't encode.
    except (OSError, ValueError, http.client.HTTPException) as exc:
        return (
            False,
            f"not ready: the API is not answering on {host}:{port} ({type(exc).__name__})",
        )
    if status != 200:
        return False, f"not ready: the status op answered {status} on {host}:{port}"
    return True, f"ready: the status op answered 200 on {host}:{port}"
