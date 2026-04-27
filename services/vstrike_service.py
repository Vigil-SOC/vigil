"""Outbound REST + MCP client for the VStrike (CloudCurrent) fusion layer.

VStrike pushes enriched findings to Vigil, but we also query it for asset
topology, adjacent-asset lookup, and blast-radius computation during
investigations. This service is consumed by `backend/api/vstrike.py` (proxy
endpoints) and `tools/vstrike.py` (MCP server).

Two auth modes are supported:

1. Bearer API key (legacy topology path) — set `VSTRIKE_API_KEY`. Used for
   `/api/v1/topology/*` and `/api/v1/findings`.
2. Username + password (new UI control / MCP path) — set `VSTRIKE_USERNAME`
   and `VSTRIKE_PASSWORD`. The service POSTs to `/mcp-login` to exchange them
   for a JSON Web Token, then uses the JWT to call MCP tools (`ui-login-token`,
   `network-list`, `ui-network-load`) at `MCP_RPC_PATH` via JSON-RPC.

Either mode (or both) is sufficient to construct the service. The
`has_api_credentials` and `has_ui_credentials` properties let callers branch.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

import requests

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 30

# MCP JSON-RPC endpoint exposed by VStrike. Confirmed live against
# https://vstrike.net — VStrike replies with `text/event-stream`.
MCP_RPC_PATH = "/mcp"

# Default JWT lifetime if VStrike doesn't tell us; refresh slightly before.
_JWT_DEFAULT_TTL_SECONDS = 50 * 60

# Module-level JWT cache so we don't re-login on every request.
# Key: (base_url, username) → (jwt, expires_at_epoch_seconds)
_jwt_cache: Dict[Tuple[str, str], Tuple[str, float]] = {}
_jwt_lock = threading.Lock()


def _parse_response_body(resp: requests.Response) -> Any:
    """Return the JSON body of a response, tolerating SSE framing.

    VStrike's MCP endpoint replies with `text/event-stream` even though the
    payload is a single JSON-RPC message. The body looks like::

        event: message
        data: {"result":...,"jsonrpc":"2.0","id":1}

    so a plain `resp.json()` fails. This helper detects SSE by content-type
    or framing, concatenates all `data:` lines, and JSON-decodes them.
    Falls back to `resp.json()` for plain JSON responses.
    """
    content_type = (resp.headers.get("Content-Type") or "").lower()
    text = resp.text
    if "text/event-stream" in content_type or text.lstrip().startswith("event:"):
        data_chunks: List[str] = []
        for line in text.splitlines():
            if line.startswith("data:"):
                data_chunks.append(line[len("data:") :].lstrip())
        if not data_chunks:
            raise ValueError("VStrike returned event-stream with no `data:` line")
        return json.loads("".join(data_chunks))
    return resp.json()


def _extract_string(data: Any, keys: Tuple[str, ...]) -> Optional[str]:
    """Pull the first matching string out of an MCP / REST response.

    Tolerates plain dicts, the MCP `tools/call` wrapping
    (`{"result": {"content": [{"type": "text", "text": "..."}]}}`) where
    the text payload may itself be JSON, the newer `structuredContent`
    field that VStrike uses for typed payloads, and one level of
    `result`/`data` nesting that some shims add.
    """
    if isinstance(data, str):
        return data or None
    if not isinstance(data, dict):
        return None

    for key in keys:
        value = data.get(key)
        if isinstance(value, str) and value:
            return value

    for wrap_key in ("result", "data", "structuredContent"):
        wrapped = data.get(wrap_key)
        if isinstance(wrapped, (dict, str)):
            found = _extract_string(wrapped, keys)
            if found:
                return found

    content = data.get("content")
    if isinstance(content, list):
        for chunk in content:
            if isinstance(chunk, dict):
                text = chunk.get("text")
                if isinstance(text, str):
                    try:
                        parsed = json.loads(text)
                    except (json.JSONDecodeError, TypeError):
                        parsed = None
                    if parsed is not None:
                        found = _extract_string(parsed, keys)
                        if found:
                            return found
    return None


def _extract_list(data: Any, keys: Tuple[str, ...]) -> Optional[List[Any]]:
    """Pull the first matching list out of an MCP / REST response."""
    if isinstance(data, list):
        return data
    if not isinstance(data, dict):
        return None

    for key in keys:
        value = data.get(key)
        if isinstance(value, list):
            return value

    for wrap_key in ("result", "data", "structuredContent"):
        wrapped = data.get(wrap_key)
        if isinstance(wrapped, (dict, list)):
            found = _extract_list(wrapped, keys)
            if found is not None:
                return found

    content = data.get("content")
    if isinstance(content, list):
        for chunk in content:
            if isinstance(chunk, dict):
                text = chunk.get("text")
                if isinstance(text, str):
                    try:
                        parsed = json.loads(text)
                    except (json.JSONDecodeError, TypeError):
                        parsed = None
                    if parsed is not None:
                        found = _extract_list(parsed, keys)
                        if found is not None:
                            return found
    return None


class VStrikeService:
    """Thin REST + MCP client for the VStrike API."""

    def __init__(
        self,
        base_url: str,
        api_key: Optional[str] = None,
        verify_ssl: bool = True,
        timeout: int = DEFAULT_TIMEOUT,
        username: Optional[str] = None,
        password: Optional[str] = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.verify_ssl = verify_ssl
        self.timeout = timeout
        self.username = username
        self.password = password

        self.session = requests.Session()
        self.session.verify = verify_ssl
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        self.session.headers.update(headers)

    # ------------------------------------------------------------------ #
    # Credential predicates (used by API layer to branch cleanly)
    # ------------------------------------------------------------------ #

    @property
    def has_api_credentials(self) -> bool:
        """True when we can authenticate to the legacy topology REST API."""
        return bool(self.api_key)

    @property
    def has_ui_credentials(self) -> bool:
        """True when we can perform mcp-login + MCP tool calls for UI control."""
        return bool(self.username and self.password)

    # ------------------------------------------------------------------ #
    # Legacy REST topology methods (Bearer api_key)
    # ------------------------------------------------------------------ #

    def _get(self, path: str, **kwargs) -> requests.Response:
        url = f"{self.base_url}{path}"
        return self.session.get(url, timeout=self.timeout, **kwargs)

    def test_connection(self) -> Tuple[bool, str]:
        """Ping the VStrike health endpoint.

        Returns a (success, message) tuple.
        """
        try:
            response = self._get("/api/v1/health")
            if response.status_code == 200:
                return True, "Connection successful"
            return False, f"HTTP {response.status_code}: {response.text[:200]}"
        except requests.exceptions.RequestException as e:
            return False, f"Connection error: {e}"

    def get_asset_topology(self, asset_id: str) -> Optional[Dict[str, Any]]:
        """Return full topology info for an asset (neighbors, segment, site)."""
        try:
            response = self._get(f"/api/v1/topology/asset/{asset_id}")
            if response.status_code == 200:
                return response.json()
            logger.warning(
                "VStrike get_asset_topology(%s) returned HTTP %s",
                asset_id,
                response.status_code,
            )
            return None
        except requests.exceptions.RequestException as e:
            logger.error("VStrike get_asset_topology(%s) failed: %s", asset_id, e)
            return None

    def list_adjacent(self, asset_id: str) -> Optional[List[Dict[str, Any]]]:
        """Return adjacent assets (one hop) for an asset."""
        try:
            response = self._get(f"/api/v1/topology/asset/{asset_id}/adjacent")
            if response.status_code == 200:
                return response.json().get("adjacent", [])
            return None
        except requests.exceptions.RequestException as e:
            logger.error("VStrike list_adjacent(%s) failed: %s", asset_id, e)
            return None

    def get_blast_radius(self, asset_id: str) -> Optional[Dict[str, Any]]:
        """Return blast-radius info (count + sample assets) for an asset."""
        try:
            response = self._get(f"/api/v1/topology/asset/{asset_id}/blast-radius")
            if response.status_code == 200:
                return response.json()
            return None
        except requests.exceptions.RequestException as e:
            logger.error("VStrike get_blast_radius(%s) failed: %s", asset_id, e)
            return None

    def find_findings_by_segment(
        self, segment: str, limit: int = 100
    ) -> Optional[List[Dict[str, Any]]]:
        """Return VStrike-enriched findings for a network segment."""
        try:
            response = self._get(
                "/api/v1/findings",
                params={"segment": segment, "limit": limit},
            )
            if response.status_code == 200:
                return response.json().get("findings", [])
            return None
        except requests.exceptions.RequestException as e:
            logger.error("VStrike find_findings_by_segment(%s) failed: %s", segment, e)
            return None

    # ------------------------------------------------------------------ #
    # MCP UI control plane (username/password → JWT → MCP tools)
    # ------------------------------------------------------------------ #

    def _mcp_login(self) -> str:
        """POST to /mcp-login and return the JWT."""
        if not (self.username and self.password):
            raise RuntimeError(
                "VStrike MCP credentials not configured "
                "(VSTRIKE_USERNAME / VSTRIKE_PASSWORD)"
            )
        url = f"{self.base_url}/mcp-login"
        try:
            resp = requests.post(
                url,
                json={"username": self.username, "password": self.password},
                timeout=self.timeout,
                verify=self.verify_ssl,
                headers={
                    "Content-Type": "application/json",
                    "Accept": "application/json, text/event-stream",
                },
            )
        except requests.exceptions.RequestException as e:
            raise RuntimeError(f"VStrike mcp-login failed: {e}") from e

        if resp.status_code != 200:
            raise RuntimeError(
                f"VStrike mcp-login HTTP {resp.status_code}: {resp.text[:200]}"
            )
        try:
            body = _parse_response_body(resp)
        except ValueError as e:
            raise RuntimeError(f"VStrike mcp-login non-JSON response: {e}") from e
        # VStrike returns the JWT under the `jsonwebtoken` key. Tolerate the
        # other names some shims might use for forward-compat.
        token = _extract_string(body, ("jsonwebtoken", "token", "jwt", "access_token"))
        if not token:
            raise RuntimeError(
                f"VStrike mcp-login response missing token field: {body!r}"
            )
        return token

    def _ensure_jwt(self) -> str:
        """Return a cached JWT or log in to fetch one."""
        if not (self.username and self.password):
            raise RuntimeError(
                "VStrike MCP credentials not configured "
                "(VSTRIKE_USERNAME / VSTRIKE_PASSWORD)"
            )
        key = (self.base_url, self.username)
        with _jwt_lock:
            cached = _jwt_cache.get(key)
            if cached and cached[1] > time.time():
                return cached[0]
        jwt = self._mcp_login()
        with _jwt_lock:
            _jwt_cache[key] = (jwt, time.time() + _JWT_DEFAULT_TTL_SECONDS)
        return jwt

    def _invalidate_jwt(self) -> None:
        if self.username:
            with _jwt_lock:
                _jwt_cache.pop((self.base_url, self.username), None)

    def _call_mcp_tool(self, tool_name: str, arguments: Dict[str, Any]) -> Any:
        """Call a VStrike MCP tool over HTTP JSON-RPC with JWT auth.

        Retries once on HTTP 401 by re-logging-in (the cached JWT may have
        expired sooner than our default TTL).
        """
        url = f"{self.base_url}{MCP_RPC_PATH}"
        payload = {
            "jsonrpc": "2.0",
            "id": int(time.time() * 1000),
            "method": "tools/call",
            "params": {"name": tool_name, "arguments": arguments},
        }

        def _post(jwt: str) -> requests.Response:
            return requests.post(
                url,
                json=payload,
                timeout=self.timeout,
                verify=self.verify_ssl,
                headers={
                    "Authorization": f"Bearer {jwt}",
                    "Content-Type": "application/json",
                    # VStrike's MCP endpoint replies as text/event-stream.
                    "Accept": "application/json, text/event-stream",
                },
            )

        jwt = self._ensure_jwt()
        try:
            resp = _post(jwt)
        except requests.exceptions.RequestException as e:
            raise RuntimeError(f"VStrike MCP {tool_name} failed: {e}") from e

        if resp.status_code == 401:
            self._invalidate_jwt()
            jwt = self._ensure_jwt()
            try:
                resp = _post(jwt)
            except requests.exceptions.RequestException as e:
                raise RuntimeError(f"VStrike MCP {tool_name} retry failed: {e}") from e

        if resp.status_code != 200:
            raise RuntimeError(
                f"VStrike MCP {tool_name} HTTP {resp.status_code}: "
                f"{resp.text[:200]}"
            )

        try:
            body = _parse_response_body(resp)
        except ValueError as e:
            raise RuntimeError(f"VStrike MCP {tool_name} non-JSON response: {e}") from e

        if isinstance(body, dict) and body.get("error"):
            raise RuntimeError(f"VStrike MCP {tool_name} error: {body['error']}")

        # JSON-RPC wraps the tool output in `result`; the tool itself may set
        # `isError: true` to signal a tool-level failure (vs. transport).
        if isinstance(body, dict) and isinstance(body.get("result"), dict):
            result = body["result"]
            if result.get("isError"):
                raise RuntimeError(
                    f"VStrike MCP {tool_name} tool error: "
                    f"{result.get('content') or result}"
                )
            return result
        return body.get("result", body) if isinstance(body, dict) else body

    def get_ui_login_token(self) -> str:
        """Return a short-lived auto-login token for the iframe URL.

        Always fetches fresh — this token is meant to be one-shot.
        """
        result = self._call_mcp_tool("ui-login-token", {})
        token = _extract_string(result, ("token", "ui_login_token", "value"))
        if not token:
            raise RuntimeError(f"VStrike ui-login-token returned no token: {result!r}")
        return token

    def list_networks(self) -> List[Dict[str, Any]]:
        """Enumerate networks visible to the configured account."""
        result = self._call_mcp_tool("network-list", {})
        networks = _extract_list(result, ("networks", "items", "data"))
        return networks or []

    def load_network_in_ui(self, network_id: str) -> Any:
        """Tell VStrike to load a given network into the active iframe.

        VStrike pushes the actual UI command to its iframe via its own
        WebSocket — this call only triggers that push.
        """
        return self._call_mcp_tool("ui-network-load", {"networkId": network_id})

    def iframe_url(self) -> str:
        """Build the auto-login iframe URL using a fresh ui-login-token."""
        token = self.get_ui_login_token()
        return f"{self.base_url}/login?token={token}"


def _config_value(key: str, config: Optional[Dict[str, Any]]) -> Optional[str]:
    if config is None:
        return None
    value = config.get(key)
    return value if isinstance(value, str) and value else None


def get_vstrike_service() -> Optional[VStrikeService]:
    """Construct a VStrikeService from env or integration config, or None.

    Configured when both `base_url` is set AND at least one credential set
    is present:

      - api_key (`VSTRIKE_API_KEY`) — enables legacy topology REST calls.
      - username + password (`VSTRIKE_USERNAME` / `VSTRIKE_PASSWORD`) —
        enables the MCP UI-control plane (iframe auto-login + ui-network-load).

    Either or both modes may be configured. Within each, env vars take
    precedence over the integration config persisted by the Settings UI.
    """
    base_url = os.environ.get("VSTRIKE_BASE_URL")
    api_key = os.environ.get("VSTRIKE_API_KEY")
    username = os.environ.get("VSTRIKE_USERNAME")
    password = os.environ.get("VSTRIKE_PASSWORD")
    verify_ssl_env = os.environ.get("VSTRIKE_VERIFY_SSL", "true").lower() != "false"

    config: Optional[Dict[str, Any]] = None
    needs_config_lookup = not base_url or not (api_key or (username and password))
    if needs_config_lookup:
        try:
            from core.config import get_integration_config

            config = get_integration_config("vstrike")
        except Exception as e:
            logger.debug("VStrike integration config not loaded: %s", e)
            config = None

    base_url = base_url or _config_value("url", config)
    api_key = api_key or _config_value("api_key", config)
    username = username or _config_value("username", config)
    password = password or _config_value("password", config)

    if not base_url:
        return None
    if not (api_key or (username and password)):
        return None

    verify_ssl = verify_ssl_env
    if config is not None and "verify_ssl" in config:
        verify_ssl = bool(config.get("verify_ssl", True))

    return VStrikeService(
        base_url=base_url,
        api_key=api_key,
        verify_ssl=verify_ssl,
        username=username,
        password=password,
    )
