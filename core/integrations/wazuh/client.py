"""Read-only client for the Wazuh manager's server API (default port 55000).

The one place Vigil calls the manager: Settings' check, the MCP tool server and
later readers (#3699) all go through ``WazuhService``. It never writes; the one
PUT is ``/logtest``, whose session is deleted afterwards.
"""

import logging
import re
from typing import Any, Dict, List, Optional, Tuple

import httpx

from core.integrations._base.proxy import integration_proxy
from core.integrations._base.tls import tls_verify

logger = logging.getLogger(__name__)

# The manager caps ``limit`` at 500 on older 4.x releases, so pages stay there.
PAGE_LIMIT = 500

# Agent ids are digits ("000" is the manager); anything else could steer the path.
_AGENT_ID = re.compile(r"^\d{3,}$")


class WazuhError(Exception):
    """A failed manager call, with a message safe to show (never a secret)."""

    def __init__(self, message: str, data: Optional[Dict[str, Any]] = None):
        super().__init__(message)
        # The body's ``data``, when the manager sent one (a logtest token).
        self.data = data or {}


class WazuhAuthError(WazuhError):
    """The manager refused the username and password."""


def _rule_ids_param(rule_ids: Any) -> Optional[str]:
    """``rule_ids`` as the manager's comma list: an int, a list of ints or digit
    strings, or a comma-separated string. None / "" / [] mean no filter."""
    if rule_ids is None:
        return None
    if isinstance(rule_ids, str):
        items: List[Any] = [p.strip() for p in rule_ids.split(",") if p.strip()]
    elif isinstance(rule_ids, (list, tuple)):
        items = list(rule_ids)
    else:
        items = [rule_ids]
    ids = []
    for item in items:
        # bool is an int in Python; refuse it with floats and the rest.
        if isinstance(item, int) and not isinstance(item, bool) and item >= 0:
            ids.append(str(item))
        elif isinstance(item, str) and re.fullmatch(r"[0-9]+", item.strip()):
            ids.append(item.strip())
        else:
            raise WazuhError(f"Invalid rule id {item!r} in rule_ids {rule_ids!r}")
    return ",".join(ids) or None


class WazuhService:
    def __init__(
        self,
        api_url: str,
        username: str,
        password: str,
        verify_ssl: bool = True,
        ca_cert_path: Optional[str] = None,
        timeout: float = 30.0,
    ):
        self.api_url = api_url.rstrip("/")
        self.username = username
        self.password = password
        self.verify_ssl = verify_ssl
        self.ca_cert_path = ca_cert_path or None
        self.timeout = timeout
        self._client: Optional[httpx.AsyncClient] = None
        self._token: Optional[str] = None

    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                base_url=self.api_url,
                verify=tls_verify(self.verify_ssl, self.ca_cert_path),
                proxy=integration_proxy("wazuh"),
                timeout=self.timeout,
            )
        return self._client

    async def close(self) -> None:
        if self._client and not self._client.is_closed:
            await self._client.aclose()

    # ------------------------------------------------------------------
    # Transport
    # ------------------------------------------------------------------

    async def _send(self, method: str, path: str, **kwargs) -> httpx.Response:
        try:
            return await self.client.request(method, path, **kwargs)
        except httpx.HTTPError as exc:
            if "CERTIFICATE_VERIFY_FAILED" in str(exc):
                raise WazuhError(
                    "Wazuh manager certificate not trusted; set the CA certificate "
                    "path or turn off SSL verification"
                ) from exc
            # The class name only: the message can carry the URL.
            raise WazuhError(
                f"Wazuh manager could not be reached ({type(exc).__name__})"
            ) from exc
        except OSError as exc:  # tls_verify: an unusable CA path, which it names
            raise WazuhError(str(exc)) from exc

    @staticmethod
    def _json(resp: httpx.Response) -> Dict[str, Any]:
        try:
            body = resp.json()
        except ValueError:
            body = None
        return body if isinstance(body, dict) else {}

    async def authenticate(self) -> None:
        resp = await self._send(
            "POST",
            "/security/user/authenticate",
            auth=httpx.BasicAuth(self.username, self.password),
        )
        if resp.status_code == 401:
            raise WazuhAuthError("Wazuh manager rejected the username or password")
        if resp.status_code >= 400:
            raise WazuhError(f"Wazuh login failed (HTTP {resp.status_code})")
        try:
            self._token = self._json(resp)["data"]["token"]
        except (KeyError, TypeError) as exc:
            raise WazuhError(
                "Wazuh login returned no token; is this the manager API?"
            ) from exc

    async def _request(self, method: str, path: str, **kwargs) -> Dict[str, Any]:
        """One authenticated call; a 401 (expired JWT) logs in again once."""
        if self._token is None:
            await self.authenticate()
        extra = kwargs.pop("headers", None) or {}
        resp = None
        for attempt in range(2):
            resp = await self._send(
                method,
                path,
                headers={**extra, "Authorization": f"Bearer {self._token}"},
                **kwargs,
            )
            if resp.status_code != 401 or attempt:
                break
            await self.authenticate()
        if resp.status_code >= 400:
            body = self._json(resp)
            detail = body.get("detail") or body.get("title") or ""
            raise WazuhError(
                f"Wazuh {method} {path} failed (HTTP {resp.status_code})"
                + (f": {detail}" if detail else ""),
                body.get("data") if isinstance(body.get("data"), dict) else None,
            )
        body = self._json(resp)
        # error != 0 with HTTP 200 means some or all items failed.
        data = body.get("data")
        data = data if isinstance(data, dict) else {}
        failed = data.get("failed_items") or []
        if body.get("error") and not data.get("affected_items") and failed:
            raise WazuhError(
                f"Wazuh {method} {path} failed: {failed[0].get('error', {}).get('message', '')}",
                data,
            )
        return data

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------

    async def test_connection(self) -> Tuple[bool, str]:
        """Log in and read ``GET /``; ``(ok, message)`` for Settings."""
        try:
            info = await self._request("GET", "/")
        except WazuhError as exc:
            return False, str(exc)
        return True, f"Connected to Wazuh API {info.get('api_version', 'unknown')}"

    async def list_rules(
        self,
        rule_ids: Any = None,
        level: Optional[str] = None,
        group: Optional[str] = None,
        search: Optional[str] = None,
        status: Optional[str] = None,
    ) -> Tuple[List[Dict[str, Any]], int]:
        """Every rule matching the filters, paged until ``total_affected_items``."""
        params: Dict[str, Any] = {"limit": PAGE_LIMIT}
        ids = _rule_ids_param(rule_ids)
        if ids:
            params["rule_ids"] = ids
        for key, value in (
            ("level", level),
            ("group", group),
            ("search", search),
            ("status", status),
        ):
            if value not in (None, ""):
                params[key] = value
        rules: List[Dict[str, Any]] = []
        total = None
        while total is None or len(rules) < total:
            data = await self._request(
                "GET", "/rules", params={**params, "offset": len(rules)}
            )
            items = data.get("affected_items") or []
            total = data.get("total_affected_items", 0)
            if not items:
                break
            rules.extend(items)
        return rules, total or 0

    async def list_agents(self) -> List[Dict[str, Any]]:
        params = {
            "limit": PAGE_LIMIT,
            "select": "id,name,status,group,os.name,os.version,os.platform,ip,version",
        }
        agents: List[Dict[str, Any]] = []
        total = None
        while total is None or len(agents) < total:
            data = await self._request(
                "GET", "/agents", params={**params, "offset": len(agents)}
            )
            items = data.get("affected_items") or []
            total = data.get("total_affected_items", 0)
            if not items:
                break
            agents.extend(items)
        return agents

    async def agent_log_collection(self, agent_id: str) -> Any:
        """The agent's active ``localfile`` entries (logcollector config)."""
        if not _AGENT_ID.match(agent_id):
            raise WazuhError(f"Not a Wazuh agent id: {agent_id!r} (digits, e.g. '000')")
        data = await self._request(
            "GET", f"/agents/{agent_id}/config/logcollector/localfile"
        )
        return data.get("localfile", data)

    async def test_event(
        self, event: str, log_format: str = "syslog", location: str = "vigil"
    ) -> Dict[str, Any]:
        """Run ``event`` through logtest, then delete the session it opened."""
        token = None
        try:
            data = await self._request(
                "PUT",
                "/logtest",
                json={"event": event, "log_format": log_format, "location": location},
            )
            token = data.get("token")
            return data
        except WazuhError as exc:
            token = exc.data.get("token")
            raise
        finally:
            if token:
                try:
                    await self._request("DELETE", f"/logtest/sessions/{token}")
                except WazuhError as exc:
                    logger.warning("Wazuh logtest session not closed: %s", exc)
