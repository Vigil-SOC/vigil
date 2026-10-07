"""CA-bundle trust for spawned MCP servers.

The tool servers run on httpx, which reads ``SSL_CERT_FILE`` / ``SSL_CERT_DIR``
and ignores ``REQUESTS_CA_BUNDLE`` / ``CURL_CA_BUNDLE`` — the names an operator
would have used to trust an internal CA while the servers ran on requests. An
on-prem MISP, PAN-OS or CAPE behind a private or inspecting CA would start
failing verification on nothing but a library swap, so the legacy names are
honored as aliases. A resolved *file* path is also exported as
``NODE_EXTRA_CA_CERTS`` so npx servers pick up the same bundle; Node has no
directory equivalent, so a capath is left as ``SSL_CERT_DIR`` only.

Forwarding also has to be explicit rather than inherited: ``stdio_client``
narrows the child environment to ``HOME``, ``LOGNAME``, ``PATH``, ``SHELL``,
``TERM``, ``USER`` plus the server's own ``env`` block, so a bundle set in the
backend's environment never reaches a server spawned that way.
"""

from __future__ import annotations

import logging
import os
from typing import Dict

logger = logging.getLogger(__name__)

# Highest precedence first. The httpx-native names win, so an operator who has
# already moved on is never overridden by a stale requests-era value.
_BUNDLE_VARS = (
    "SSL_CERT_FILE",
    "SSL_CERT_DIR",
    "REQUESTS_CA_BUNDLE",
    "CURL_CA_BUNDLE",
)


def ca_bundle_env() -> Dict[str, str]:
    """The CA-bundle entries to merge into a spawned MCP server's environment.

    Empty when no bundle is configured, which leaves httpx on certifi.
    """
    for var in _BUNDLE_VARS:
        value = os.environ.get(var)  # noqa: ENV001 - MCP child process env
        if not value:
            continue
        if not os.path.exists(value):
            logger.warning(
                "%s points at %s, which does not exist — MCP tool servers will "
                "fall back to the certifi bundle",
                var,
                value,
            )
            return {}
        # httpx checks SSL_CERT_FILE before SSL_CERT_DIR, and cafile vs capath
        # are not interchangeable, so route by what the path actually is.
        if os.path.isdir(value):
            return {"SSL_CERT_DIR": value}
        return {"SSL_CERT_FILE": value, "NODE_EXTRA_CA_CERTS": value}
    return {}
