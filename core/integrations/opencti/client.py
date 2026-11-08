"""Read-only client for the OpenCTI GraphQL API (``POST {url}/graphql``).

The one place Vigil calls OpenCTI: Settings' check and the MCP tool server both
go through ``OpenCTIService``. It only ever sends queries, never mutations.
"""

import logging
import re
from typing import Any, Dict, List, Optional, Tuple

import httpx

from core.integrations._base.proxy import integration_proxy
from core.integrations._base.tls import tls_verify

logger = logging.getLogger(__name__)

# Every list is capped so a graph walk cannot flood an agent's context.
MAX_LIMIT = 50

# OpenCTI ids are UUIDs (internal ids) or STIX ids ("indicator--<uuid>").
_ID = re.compile(r"^[A-Za-z0-9_\-]{8,80}$")

ENTITY_QUERIES = {
    "threat_actor": "threatActorsGroup",
    "intrusion_set": "intrusionSets",
    "malware": "malwares",
    "attack_pattern": "attackPatterns",
    "tool": "tools",
    "campaign": "campaigns",
    "report": "reports",
}

_OBSERVABLE_FIELDS = """
  id entity_type observable_value x_opencti_score
  objectLabel { value }
  indicators(first: 10) {
    edges { node { id name pattern valid_from valid_until x_opencti_score } }
  }
"""

_ENTITY_FIELDS = """
  id entity_type name description confidence
  objectLabel { value }
"""

# Fields only some entity types have; asking another type for them fails.
# x_mitre_id is the ATT&CK technique id, as analyze_coverage takes it.
_EXTRA_ENTITY_FIELDS = {"attack_pattern": " x_mitre_id"}


# A relationship target or container object: a named entity or an observable.
_TARGET_FIELDS = """
  ... on StixDomainObject { id entity_type representative { main } }
  ... on AttackPattern { x_mitre_id }
  ... on StixCyberObservable { id entity_type observable_value }
"""


class OpenCTIError(Exception):
    """A failed OpenCTI call, with a message safe to show (never the token)."""


def _limit(value: Any) -> int:
    try:
        n = int(value)
    except (TypeError, ValueError):
        return 10
    return max(1, min(n, MAX_LIMIT))


def _nodes(connection: Any) -> List[Dict[str, Any]]:
    edges = (connection or {}).get("edges") or []
    return [e["node"] for e in edges if isinstance(e, dict) and e.get("node")]


def _target(node: Dict[str, Any]) -> Dict[str, Any]:
    """A relationship target or container object as one shape: its id, type and
    name (a named entity's representative name, or an observable's value), and
    an attack pattern's ATT&CK id."""
    named = (node.get("representative") or {}).get("main")
    out = {
        "id": node.get("id"),
        "entity_type": node.get("entity_type"),
        "name": named if named is not None else node.get("observable_value"),
    }
    if node.get("x_mitre_id"):
        out["x_mitre_id"] = node["x_mitre_id"]
    return out


class OpenCTIService:
    def __init__(
        self,
        url: str,
        api_token: str,
        verify_ssl: bool = True,
        ca_cert_path: Optional[str] = None,
        timeout: float = 30.0,
    ):
        self.url = url.rstrip("/")
        self.api_token = api_token
        self.verify_ssl = verify_ssl
        self.ca_cert_path = ca_cert_path or None
        self.timeout = timeout
        self._client: Optional[httpx.AsyncClient] = None

    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                verify=tls_verify(self.verify_ssl, self.ca_cert_path),
                proxy=integration_proxy("opencti"),
                timeout=self.timeout,
            )
        return self._client

    async def close(self) -> None:
        if self._client and not self._client.is_closed:
            await self._client.aclose()

    async def _query(
        self, query: str, variables: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        try:
            resp = await self.client.post(
                f"{self.url}/graphql",
                headers={"Authorization": f"Bearer {self.api_token}"},
                json={"query": query, "variables": variables or {}},
            )
        except httpx.HTTPError as exc:
            if "CERTIFICATE_VERIFY_FAILED" in str(exc):
                raise OpenCTIError(
                    "OpenCTI certificate not trusted; set the CA certificate "
                    "path or turn off SSL verification"
                ) from exc
            # The class name only: the message can carry the URL.
            raise OpenCTIError(
                f"OpenCTI could not be reached ({type(exc).__name__})"
            ) from exc
        except OSError as exc:  # tls_verify: an unusable CA path, which it names
            raise OpenCTIError(str(exc)) from exc
        if resp.status_code in (401, 403):
            raise OpenCTIError("OpenCTI rejected the API token")
        if resp.status_code >= 400:
            raise OpenCTIError(f"OpenCTI request failed (HTTP {resp.status_code})")
        try:
            body = resp.json()
        except ValueError as exc:
            raise OpenCTIError(
                "OpenCTI returned no JSON; is this the platform URL?"
            ) from exc
        errors = body.get("errors") if isinstance(body, dict) else None
        if errors:
            first = errors[0] if isinstance(errors[0], dict) else {}
            message = first.get("message", "query failed")
            if "must be authenticated" in message.lower() or (
                (first.get("extensions") or {}).get("code") == "AUTH_REQUIRED"
            ):
                raise OpenCTIError("OpenCTI rejected the API token")
            raise OpenCTIError(f"OpenCTI query failed: {message}")
        data = body.get("data") if isinstance(body, dict) else None
        return data if isinstance(data, dict) else {}

    async def test_connection(self) -> Tuple[bool, str]:
        """``(ok, message)`` for Settings: the version, read with the token."""
        try:
            data = await self._query("query { about { version } }")
        except OpenCTIError as exc:
            return False, str(exc)
        version = (data.get("about") or {}).get("version")
        if not version:
            return False, "OpenCTI returned no version; is this the platform URL?"
        return True, f"Connected to OpenCTI {version}"

    async def search_observable(self, value: str, limit: Any = 10) -> Dict[str, Any]:
        data = await self._query(
            "query($s: String, $n: Int) { stixCyberObservables(search: $s, first: $n)"
            " { edges { node {" + _OBSERVABLE_FIELDS + "} } } }",
            {"s": value, "n": _limit(limit)},
        )
        observables = _nodes(data.get("stixCyberObservables"))
        for obs in observables:
            obs["indicators"] = _nodes(obs.get("indicators"))
        # Search is fuzzy; say which results are the exact value asked for.
        wanted = value.strip().lower()
        for obs in observables:
            obs["exact_match"] = (obs.get("observable_value") or "").lower() == wanted
        return {
            "value": value,
            "found": any(o["exact_match"] for o in observables),
            "count": len(observables),
            "observables": observables,
        }

    async def get_indicator(self, indicator_id: str) -> Dict[str, Any]:
        if not _ID.match(indicator_id or ""):
            raise OpenCTIError(f"Invalid indicator id {indicator_id!r}")
        data = await self._query(
            "query($id: String!) { indicator(id: $id) {"
            " id name description pattern pattern_type valid_from valid_until"
            " x_opencti_score confidence revoked"
            " objectLabel { value } killChainPhases { kill_chain_name phase_name }"
            " } }",
            {"id": indicator_id},
        )
        indicator = data.get("indicator")
        if not indicator:
            return {"found": False, "id": indicator_id}
        return {"found": True, "indicator": indicator}

    async def search_entities(
        self, query: str, entity_type: str, limit: Any = 10
    ) -> Dict[str, Any]:
        field = ENTITY_QUERIES.get(entity_type)
        if field is None:
            raise OpenCTIError(
                f"Unknown entity_type {entity_type!r}; use one of "
                + ", ".join(sorted(ENTITY_QUERIES))
            )
        # No query lists the newest-updated first (recent intel); a named
        # search keeps OpenCTI's relevance order.
        order = "" if query else ", orderBy: updated_at, orderMode: desc"
        data = await self._query(
            f"query($s: String, $n: Int) {{ {field}(search: $s, first: $n{order})"
            " { edges { node {"
            + _ENTITY_FIELDS
            + _EXTRA_ENTITY_FIELDS.get(entity_type, "")
            + "} } } }",
            {"s": query, "n": _limit(limit)},
        )
        entities = _nodes(data.get(field))
        return {
            "query": query,
            "entity_type": entity_type,
            "count": len(entities),
            "entities": entities,
        }

    async def get_related(self, entity_id: str, limit: Any = 20) -> Dict[str, Any]:
        if not _ID.match(entity_id or ""):
            raise OpenCTIError(f"Invalid entity id {entity_id!r}")
        # A container's (report, grouping, case) contents are object refs, not
        # relationships; ask for both in one round trip.
        data = await self._query(
            "query($id: String!, $ids: [String], $n: Int) {"
            " stixCoreRelationships(fromId: $ids, first: $n) {"
            " edges { node { relationship_type confidence"
            " to {" + _TARGET_FIELDS + "} } } }"
            " stixDomainObject(id: $id) { ... on Container { objects(first: $n) {"
            " pageInfo { globalCount }"
            " edges { node {" + _TARGET_FIELDS + "} } } } } }",
            {"id": entity_id, "ids": [entity_id], "n": _limit(limit)},
        )
        relationships = [
            {**r, "to": _target(r.get("to") or {})}
            for r in _nodes(data.get("stixCoreRelationships"))
        ]
        out: Dict[str, Any] = {
            "id": entity_id,
            "count": len(relationships),
            "relationships": relationships,
        }
        container = data.get("stixDomainObject") or {}
        if "objects" in container:
            # Relationships a report holds match neither fragment and come back empty.
            objects = [_target(o) for o in _nodes(container["objects"]) if o.get("id")]
            # object_count is what the container holds, not the capped page: a
            # report of 9 read with limit=3 says 9, with 3 objects listed.
            total = ((container["objects"] or {}).get("pageInfo") or {}).get(
                "globalCount"
            )
            out["object_count"] = total if isinstance(total, int) else len(objects)
            out["objects"] = objects
        return out


def configured_service(
    timeout: float = 30.0,
) -> Tuple[Optional["OpenCTIService"], tuple]:
    """A service on the stored OpenCTI settings, or the names of the fields still
    missing."""
    from core.integrations._base.config import missing, resolve
    from core.integrations.opencti.descriptor import OPENCTI, OPENCTI_REQUIRED

    config = resolve(OPENCTI)
    absent = missing(config, *OPENCTI_REQUIRED)
    if absent:
        return None, absent
    # resolve() returns every declared field; an unset verify_ssl means verify.
    verify = True if config.get("verify_ssl") is None else config["verify_ssl"]
    return (
        OpenCTIService(
            url=str(config["url"]),
            api_token=str(config["api_token"]),
            verify_ssl=bool(verify),
            ca_cert_path=config.get("ca_cert_path"),
            timeout=timeout,
        ),
        (),
    )
