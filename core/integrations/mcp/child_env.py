"""CA-bundle trust and egress proxy for spawned MCP servers.

The tool servers run on httpx, which reads ``SSL_CERT_FILE`` / ``SSL_CERT_DIR``
and ignores ``REQUESTS_CA_BUNDLE`` / ``CURL_CA_BUNDLE`` — the names an operator
would have used to trust an internal CA while the servers ran on requests. An
on-prem MISP, PAN-OS or CAPE behind a private or inspecting CA would start
failing verification on nothing but a library swap, so the legacy names are
honored as aliases. A resolved *file* path is also exported as
``NODE_EXTRA_CA_CERTS`` so npx servers pick up the same bundle; Node has no
directory equivalent, so a capath is left as ``SSL_CERT_DIR`` only.

Forwarding also has to be explicit rather than inherited: ``stdio_client``
merges the ``env`` it is given over ``HOME``, ``LOGNAME``, ``PATH``, ``SHELL``,
``TERM`` and ``USER``, so it only narrows the child if that ``env`` is narrow.
``stdio_env`` builds it: the inputs the MCP runner would start the server with,
plus a few stdio-only additions, and none of the rest of the backend's
environment.

The same holds for the proxy variables: on a proxy-only network a server
spawned without them reports running while every tool call fails to connect.
Node's ``fetch`` ignores them even when present unless ``NODE_USE_ENV_PROXY`` is
set (22.21+), so that flag rides along whenever a proxy does.
"""

from __future__ import annotations

import logging
import os
import re
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


_PROXY_VARS = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY")
PROXY_ENV_NAMES = frozenset(
    spelling for var in (*_PROXY_VARS, "NO_PROXY") for spelling in (var, var.lower())
)


def proxy_env() -> Dict[str, str]:
    """The egress-proxy entries to merge into a spawned MCP server's environment.

    Both spellings are forwarded as set. Empty when no proxy is configured (a
    lone ``NO_PROXY`` bypasses nothing), so a deployment without one sees an
    unchanged child environment.
    """
    env = {
        name: value
        for name in sorted(PROXY_ENV_NAMES)
        if (value := os.environ.get(name))  # noqa: ENV001 - MCP child process env
    }
    if not any(name.upper() in _PROXY_VARS for name in env):
        return {}
    env["NODE_USE_ENV_PROXY"] = "1"
    return env


# Package launchers' own settings (registry, index, cache): a runner-less install
# downloads pinned npx/uvx entries at spawn, so a mirror set in the backend's
# environment has to reach them. Matched case-insensitively by prefix.
_LAUNCHER_PREFIXES = ("NPM_CONFIG_", "UV_", "PIP_")

# What an in-repo server reads to reach the database and the secret store the way
# the backend does (resolve() reads its non-secret settings from the database).
_IN_REPO_VARS = (
    "POSTGRES_HOST",
    "POSTGRES_PORT",
    "POSTGRES_DB",
    "POSTGRES_USER",
    "POSTGRES_PASSWORD",
    "POSTGRES_SSL_MODE",
    "DEV_MODE",
    "SECRETS_BACKEND",
    "ENABLE_KEYRING",
    "VIGIL_DISABLE_DOTENV",
    "VIGIL_DIR_EPHEMERAL",
)

# Credential-file locations (paths, not keys) for the cloud entries that sign in
# from a file.
_CLOUD_FILE_VARS = (
    "GOOGLE_APPLICATION_CREDENTIALS",
    "AWS_SHARED_CREDENTIALS_FILE",
    "AWS_CONFIG_FILE",
    "AWS_PROFILE",
)

# Credential pointers for the Security Lake server: where its default AWS chain
# finds credentials (files, profile, web identity, container endpoint), never a key.
SECURITY_LAKE_SERVER = "amazon-security-lake"
AWS_POINTER_VARS = (
    "AWS_SHARED_CREDENTIALS_FILE",
    "AWS_CONFIG_FILE",
    "AWS_PROFILE",
    "AWS_ROLE_ARN",
    "AWS_WEB_IDENTITY_TOKEN_FILE",
    "AWS_CONTAINER_CREDENTIALS_FULL_URI",
    "AWS_CONTAINER_CREDENTIALS_RELATIVE_URI",
    "AWS_CONTAINER_AUTHORIZATION_TOKEN_FILE",
    "AWS_REGION",
    "AWS_DEFAULT_REGION",
)


def aws_pointer_env(server_name: str) -> Dict[str, str]:
    """The AWS credential pointers set here, for the Security Lake server only."""
    if server_name != SECURITY_LAKE_SERVER:
        return {}
    return {
        name: value
        for name in AWS_POINTER_VARS
        if (value := os.environ.get(name))  # noqa: ENV001 - MCP child process env
    }


_IN_REPO_ARG = re.compile(r"core/integrations/[^/]+/tool\.py")


def stdio_env(server, project_root: str) -> Dict[str, str]:
    """The env a stdio-spawned ``server`` starts with, built at connect time.

    The runner's inputs (``runner_env``), the proxy and CA-bundle entries and
    ``PYTHONPATH``, as ``Catalog.launch`` adds them, plus what only a runner-less
    child needs: ``VIGIL_DIR`` (it shares the backend's state directory), the
    package launchers' settings, for in-repo servers the database and
    secret-store variables, for ``gcp-*`` / ``aws-security`` the credential
    file locations, and for ``amazon-security-lake`` its AWS credential pointers. Each extra is passed only when set in the backend. Anything
    else in the backend's environment, raw cloud keys included, stays there.
    """
    from core.config import vigil_path
    from core.integrations.mcp.runner import runner_env

    # Declared keys reach the child even when empty, as under the runner, whose
    # catalog launches the entry's own env.
    env = {k: (server.env or {}).get(k, "") for k in server.declared_env}
    env.update(runner_env(server))
    # A proxy name the entry sets drops both forwarded spellings.
    overridden = {k.upper() for k in env if k in PROXY_ENV_NAMES}
    env = {
        **{k: v for k, v in proxy_env().items() if k.upper() not in overridden},
        **env,
        **ca_bundle_env(),
    }
    env["PYTHONPATH"] = project_root
    env["VIGIL_DIR"] = str(vigil_path())

    extra = [
        name
        for name in os.environ  # noqa: ENV001 - MCP child process env
        if name.upper().startswith(_LAUNCHER_PREFIXES)
    ]
    if any(_IN_REPO_ARG.fullmatch(arg) for arg in server.args):
        extra += _IN_REPO_VARS
    if server.name.startswith("gcp-") or server.name == "aws-security":
        extra += _CLOUD_FILE_VARS
    for name in extra:
        if value := os.environ.get(name):  # noqa: ENV001 - MCP child process env
            env.setdefault(name, value)
    for name, value in aws_pointer_env(server.name).items():
        env.setdefault(name, value)
    return env
