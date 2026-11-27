"""Declared remote MCP sources (#5302): ``sources/<name>.md`` in the declared directory.

The single home of a declared source: the strict parser, the name rule, the
launch hash, the cached read of the ``sources.applied`` row the daemon writes,
acceptance, and the view. Nothing here connects to a source.

Each file's YAML front matter holds three keys and nothing else:

- ``url``: a literal https URL (no template, no ``$``, no userinfo);
- ``headers``: header name -> literal text, where ``{{field}}`` names one of
  the file's own credentials; protocol headers are refused;
- ``credentials``: field names of up to 32 lowercase letters and digits,
  stored as ``SOURCE_<NAME>_<FIELD>`` under integration id ``source-<name>``.

The body is for people. The file stem is the name. A refusal anywhere rejects
the whole revision with file and line; the last applied entries stay.

The row: ``sources`` (name -> entry) as last applied, and ``accepted`` (name
-> the launch a person accepted). A source is live only while its launch hash
equals its accepted one; removal and a body change apply at once.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple
from urllib.parse import urlsplit

import yaml

from core.frontmatter import (
    FrontmatterError,
    frontmatter_alias_line,
    frontmatter_node,
    split_frontmatter,
)
from core.intent import IntentRejected
from core.time import utcnow

logger = logging.getLogger(__name__)

SOURCES_APPLIED_KEY = "sources.applied"
SOURCES_DIR_NAME = "sources"
INTEGRATION_PREFIX = "source-"
# The ceiling on one revision. Constants on purpose: no setting, no env var.
MAX_SOURCES_BYTES = 256 * 1024
MAX_SOURCES_FILES = 64

KEYS = ("url", "headers", "credentials")
NAME = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
MAX_NAME = 63
FIELD = re.compile(r"^[a-z0-9]{1,32}$")
HEADER_NAME = re.compile(r"^[A-Za-z0-9-]{1,64}$")
TEMPLATE = re.compile(r"\{\{([a-z0-9]{1,32})\}\}")
MAX_URL = 2048
MAX_HEADER_VALUE = 4096
# Headers the HTTP client or the MCP transport sets; a source may not.
PROTOCOL_HEADERS = frozenset(
    {
        "accept",
        "accept-encoding",
        "connection",
        "content-encoding",
        "content-length",
        "content-type",
        "cookie",
        "expect",
        "host",
        "keep-alive",
        "last-event-id",
        "te",
        "trailer",
        "transfer-encoding",
        "upgrade",
    }
)
PROTOCOL_PREFIXES = ("mcp-", "proxy-", "sec-")

NOTHING_DECLARED = "Nothing is declared: VIGIL_INTENT_DIR is not set"

SourceRejected = IntentRejected  # file, line, reason


class SourcesRefused(Exception):
    """An accept that cannot apply: ``status_code`` and ``detail`` for the route."""

    def __init__(self, status_code: int, detail: str):
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


# --- Names -------------------------------------------------------------------


def catalogue_names() -> frozenset[str]:
    """The baked ``mcp-config.json`` server names, read as the runner and
    ``verify_catalog`` read them (core may not import the runner)."""
    root = Path(__file__).resolve().parents[1]
    raw = json.loads((root / "mcp-config.json").read_text())["mcpServers"]
    # String values are the file's _comment_* section headers.
    return frozenset(k for k, v in raw.items() if isinstance(v, dict))


def reserved_names(own_tools: Iterable[str]) -> Dict[str, str]:
    """Name -> why a source may not take it: a catalogue server, ``vigil``, or
    the leading segment of a tool Vigil answers itself or emulates with.

    ``own_tools`` is ``own_tool_names()`` and ``ATOMIC_TOOLS``, passed in by
    the caller: core.response reaches the integrations core.sources serves.
    """
    reserved = {
        n.split("_", 1)[0]: f"the leading segment of Vigil's own tool {n!r}"
        for n in sorted(own_tools, reverse=True)
    }
    reserved["vigil"] = "Vigil's own server"
    reserved.update({n: "a catalogue server" for n in catalogue_names()})
    return reserved


def name_refusal(name: str, reserved: Dict[str, str]) -> Optional[str]:
    if len(name) > MAX_NAME or not NAME.match(name):
        return (
            f"source name {name!r} must be lowercase letters, digits and single "
            f"hyphens, at most {MAX_NAME} characters, with no underscore"
        )
    if name in reserved:
        return f"source name {name!r} is taken: {reserved[name]}"
    return None


def integration_id(name: str) -> str:
    return f"{INTEGRATION_PREFIX}{name}"


def credential_key(name: str, field: str) -> str:
    """``SOURCE_<NAME>_<FIELD>``, hyphens as underscores."""
    from core.integrations.integration_secrets import default_env_var

    return default_env_var(integration_id(name), field)


# --- Parser ------------------------------------------------------------------


def _mapping(node: Any) -> Dict[str, Tuple[Any, Any]]:
    if not isinstance(node, yaml.MappingNode):
        return {}
    return {str(k.value): (k, v) for k, v in node.value}


def _check_url(url: Any) -> Optional[str]:
    if not isinstance(url, str) or not url:
        return "url must be text"
    if len(url) > MAX_URL:
        return f"url is longer than {MAX_URL} characters"
    if "$" in url or "{{" in url or "}}" in url:
        return "url must be literal: no template and no '$'"
    if any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in url):
        return "url may not hold whitespace or control characters"
    if not url.startswith("https://"):
        return "url must be https"
    try:
        parts = urlsplit(url)
        parts.port  # noqa: B018 - raises on a bad port
    except ValueError as exc:
        return f"url does not parse: {exc}"
    if "@" in parts.netloc:
        return "url may not carry userinfo"
    if not parts.hostname:
        return "url names no host"
    return None


def _check_header(name: Any, value: Any, fields: Sequence[str]) -> Optional[str]:
    if not isinstance(name, str) or not HEADER_NAME.match(name):
        return f"header name {name!r} must be letters, digits and hyphens"
    lowered = name.lower()
    if lowered in PROTOCOL_HEADERS or lowered.startswith(PROTOCOL_PREFIXES):
        return f"header {name!r} is a protocol header; the client sets it"
    if not isinstance(value, str) or not value.strip():
        return f"header {name!r}: value must be text"
    if len(value) > MAX_HEADER_VALUE:
        return f"header {name!r}: value is longer than {MAX_HEADER_VALUE} characters"
    if "$" in value:
        return (
            f"header {name!r}: '$' is not allowed; name a credential as {{{{field}}}}"
        )
    if any(ord(c) < 32 or ord(c) == 127 for c in value):
        return f"header {name!r}: control characters are not allowed"
    for field in TEMPLATE.findall(value):
        if field not in fields:
            return (
                f"header {name!r}: {{{{{field}}}}} is not one of this file's "
                f"credentials ({', '.join(fields) or 'none declared'})"
            )
    rest = TEMPLATE.sub("", value)
    if "{" in rest or "}" in rest:
        return (
            f"header {name!r}: a template may only be {{{{field}}}}, naming one of "
            "this file's credentials"
        )
    return None


def parse_file(name: str, content: str, reserved: Dict[str, str]) -> Dict[str, Any]:
    """One source, or :class:`SourceRejected`. ``name`` is ``sources/<stem>.md``."""
    stem = name.rsplit("/", 1)[-1][: -len(".md")]
    refused = name_refusal(stem, reserved)
    if refused:
        raise SourceRejected(name, 1, refused)
    try:
        front, offset = split_frontmatter(content)
    except FrontmatterError as exc:
        problem = getattr(exc.__cause__, "problem", None)
        raise SourceRejected(
            name, exc.line, f"invalid YAML: {problem}" if problem else str(exc)
        ) from exc
    if front is None:
        raise SourceRejected(name, 1, "no YAML front matter")
    alias = frontmatter_alias_line(content)
    if alias:
        raise SourceRejected(name, alias, "YAML anchors and aliases are not allowed")
    root, first = frontmatter_node(content)

    def line(node: Any) -> int:
        return first + node.start_mark.line if node is not None else first

    def refuse_duplicates(node: Any) -> None:
        if isinstance(node, yaml.MappingNode):
            seen: set = set()
            for key_node, value_node in node.value:
                if key_node.value in seen:
                    raise SourceRejected(
                        name, line(key_node), f"duplicate key {key_node.value!r}"
                    )
                seen.add(key_node.value)
                refuse_duplicates(value_node)
        elif isinstance(node, yaml.SequenceNode):
            for child in node.value:
                refuse_duplicates(child)

    refuse_duplicates(root)
    keys = _mapping(root)
    for key in front:
        if key not in KEYS:
            raise SourceRejected(
                name,
                line(keys.get(str(key), (None,))[0]),
                f"unknown key {key!r}; expected one of {', '.join(KEYS)}",
            )

    def at(key: str) -> int:
        return line(keys.get(key, (None, None))[1])

    if "url" not in front:
        raise SourceRejected(name, first, "url is missing")
    url = front["url"]
    if refused := _check_url(url):
        raise SourceRejected(name, at("url"), refused)

    credentials = front.get("credentials")
    credentials = [] if credentials is None else credentials
    if not isinstance(credentials, list):
        raise SourceRejected(name, at("credentials"), "credentials must be a list")
    cred_node = keys.get("credentials", (None, None))[1]
    for index, field in enumerate(credentials):
        where = line(cred_node.value[index]) if cred_node is not None else first
        if not isinstance(field, str) or not FIELD.match(field):
            raise SourceRejected(
                name,
                where,
                f"credential {field!r} must be 1 to 32 lowercase letters and digits",
            )
        if field in credentials[:index]:
            raise SourceRejected(name, where, f"credential {field!r} is declared twice")

    headers = front.get("headers")
    headers = {} if headers is None else headers
    if not isinstance(headers, dict):
        raise SourceRejected(name, at("headers"), "headers must be a mapping")
    header_keys = _mapping(keys.get("headers", (None, None))[1])
    seen: Dict[str, str] = {}
    for header, value in headers.items():
        where = line(header_keys.get(str(header), (None, None))[0])
        if refused := _check_header(header, value, credentials):
            raise SourceRejected(name, where, refused)
        if header.lower() in seen:
            raise SourceRejected(
                name, where, f"header {header!r} repeats {seen[header.lower()]!r}"
            )
        seen[header.lower()] = header

    return {
        "name": stem,
        "file": name,
        "url": url,
        "headers": dict(headers),
        "credentials": list(credentials),
        "body": content[offset:].strip(),
    }


def parse_sources(
    files: Sequence[Tuple[str, str]], own_tools: Iterable[str]
) -> Dict[str, Dict[str, Any]]:
    """Name -> source of one revision, or :class:`SourceRejected` for all of it.

    ``files`` is ``(sources/<stem>.md, text)`` in name order; ``own_tools`` as
    :func:`reserved_names` takes them.
    """
    if len(files) > MAX_SOURCES_FILES:
        raise SourceRejected(
            SOURCES_DIR_NAME,
            1,
            f"{len(files)} files is over the ceiling of {MAX_SOURCES_FILES}",
        )
    size = sum(len(text.encode("utf-8")) for _, text in files)
    if size > MAX_SOURCES_BYTES:
        raise SourceRejected(
            SOURCES_DIR_NAME,
            1,
            f"{size} bytes is over the ceiling of {MAX_SOURCES_BYTES} bytes",
        )
    reserved = reserved_names(own_tools) if files else {}
    sources: Dict[str, Dict[str, Any]] = {}
    for name, text in files:
        source = parse_file(name, text, reserved)
        source["launch_hash"] = launch_hash(source)
        sources[source["name"]] = source
    return sources


def launch_hash(source: Dict[str, Any]) -> str:
    """Full SHA-256 of the URL, headers and credential names, canonical JSON."""
    launch = {
        "url": source["url"],
        "headers": dict(sorted((source.get("headers") or {}).items())),
        "credentials": sorted(source.get("credentials") or []),
    }
    return hashlib.sha256(
        json.dumps(launch, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


# --- The applied row ---------------------------------------------------------

_APPLIED_TTL_SECONDS = 10.0
_applied_cache: Tuple[float, Dict[str, Any]] = (float("-inf"), {})


def applied_sources(*, fresh: bool = False) -> Dict[str, Any]:
    """The ``sources.applied`` row through a short cache; ``{}`` when absent.

    A failed read keeps the last value read.
    """
    global _applied_cache
    now = time.monotonic()
    at, value = _applied_cache
    if fresh or now - at >= _APPLIED_TTL_SECONDS:
        try:
            from core.storage.config_service import get_config_service

            row = get_config_service().read_system_config(SOURCES_APPLIED_KEY)
            value = row if isinstance(row, dict) else {}
        except Exception as exc:  # noqa: BLE001
            logger.debug("sources.applied not read; keeping last: %s", exc)
        _applied_cache = (now, value)
    return value


def update_applied(
    change: Callable[[Dict[str, Any], Any], Tuple[Optional[Dict[str, Any]], str]],
    actor: str,
) -> Dict[str, Any]:
    """Read, change and write ``sources.applied`` in one transaction, row locked.

    The daemon's poll and a person's accept both go through here, so neither
    loses the other's write. ``change`` gets the row and the session (for class
    row deletes in the same transaction) and returns ``(row or None, reason)``.
    """
    from sqlalchemy import select

    from core.storage import config_service as config_store
    from core.storage.models import SystemConfig
    from core.storage.unit_of_work import unit_of_work

    with unit_of_work() as session:
        row = session.execute(
            select(SystemConfig)
            .where(SystemConfig.key == SOURCES_APPLIED_KEY)
            .with_for_update()
        ).scalar_one_or_none()
        current = dict(row.value) if row is not None and row.value else {}
        updated, reason = change(current, session)
        if updated is None or updated == current:
            return current
        if row is None:
            session.add(
                SystemConfig(
                    key=SOURCES_APPLIED_KEY,
                    value=updated,
                    description="Declared sources/ revision applied by the daemon",
                    config_type="intent",
                    updated_by=actor,
                )
            )
        else:
            row.value = updated
            row.updated_by = actor
        config_store.ConfigService(user_id=actor)._create_audit_log(
            session,
            config_type="intent",
            config_key=SOURCES_APPLIED_KEY,
            action="create" if row is None else "update",
            old_value=current or None,
            new_value=updated,
            change_reason=reason,
        )
    applied_sources(fresh=True)
    return updated


def is_accepted(name: str, row: Dict[str, Any]) -> bool:
    source = (row.get("sources") or {}).get(name)
    accepted = (row.get("accepted") or {}).get(name)
    return bool(source and accepted and accepted.get("hash") == source["launch_hash"])


def pending_change(name: str, row: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """What a pending source alters against the launch a person accepted."""
    if is_accepted(name, row):
        return None
    source = (row.get("sources") or {})[name]
    accepted = (row.get("accepted") or {}).get(name)
    if not accepted:
        return {"kind": "new", "changes": ["url", "headers", "credentials"]}
    changes = [
        key
        for key, a, b in (
            ("url", accepted.get("url"), source["url"]),
            ("headers", accepted.get("headers") or {}, source["headers"]),
            (
                "credentials",
                sorted(accepted.get("credentials") or []),
                sorted(source["credentials"]),
            ),
        )
        if a != b
    ]
    change: Dict[str, Any] = {"kind": "changed", "changes": changes}
    if "url" in changes:
        change["url_from"] = accepted.get("url")
        change["url_to"] = source["url"]
        change["asks_credentials_again"] = True
    if "headers" in changes:
        old, new = accepted.get("headers") or {}, source["headers"]
        change["headers_changed"] = sorted(
            h for h in set(old) | set(new) if old.get(h) != new.get(h)
        )
    if "credentials" in changes:
        old_c, new_c = set(accepted.get("credentials") or []), set(
            source["credentials"]
        )
        change["credentials_added"] = sorted(new_c - old_c)
        change["credentials_removed"] = sorted(old_c - new_c)
    return change


# --- Class rows and credentials ----------------------------------------------


def stored_credential_keys(name: str) -> List[str]:
    """``SOURCE_<NAME>_<FIELD>`` keys held in the encrypted store for ``name``.

    Matched exactly, so ``a`` never reaches ``a-b``'s keys (fields hold no ``_``).
    """
    from core.secrets_manager import get_secrets_manager

    backend = get_secrets_manager().encrypted_backend
    if not backend.is_available():
        return []
    prefix = credential_key(name, "")
    pattern = re.compile(re.escape(prefix) + r"[A-Z0-9]{1,32}")
    return sorted(k for k in backend._load_cache() if pattern.fullmatch(k))


def record_credential_deletes(
    session, name: str, actor: str, why: str, keys: Optional[Sequence[str]] = None
) -> List[str]:
    """One trail entry per stored credential of ``name`` (never a value).

    ``keys`` defaults to the source's own; an integration instance passes its
    own (#5353). The keys are returned for :func:`delete_stored`, which runs
    once every entry is recorded, so a refused trail deletes nothing.
    """
    from core.auth import trail_events

    keys = stored_credential_keys(name) if keys is None else sorted(keys)
    for key in keys:
        trail_events.record(
            session,
            trail_events.SOURCE_CREDENTIAL_DELETED,
            actor=actor,
            source=name,
            key=key,
            reason=why,
        )
    return keys


def delete_stored(keys: Sequence[str]) -> None:
    """Delete stored credentials; one that stays rolls the accept back."""
    from core.secrets import delete_secret

    for key in keys:
        if not delete_secret(key):
            raise SourcesRefused(500, f"The stored credential {key} was not deleted")


def source_secret_fields(integration: str) -> Dict[str, str]:
    """Field -> key for ``source-<name>``: the applied row's declared fields only.

    Only an accepted source answers: accepting a new source or a URL move
    deletes its stored credentials, so one entered while pending would be lost.
    """
    name = integration[len(INTEGRATION_PREFIX) :]
    row = applied_sources()
    source = (row.get("sources") or {}).get(name)
    if not source or not is_accepted(name, row):
        return {}
    return {f: credential_key(name, f) for f in source.get("credentials") or []}


# --- Accepting ---------------------------------------------------------------


def accept_sources(
    names: Sequence[str],
    revision: str,
    digest: str,
    user_id: str,
    who: str,
    delete_tool_classes: Callable[[Any, str, str], List[str]],
) -> Dict[str, Any]:
    """A person's accept of pending sources, checked again on the locked row.

    A new source drops any class row and stored credential under its name; an
    accepted URL move drops its stored credentials. Each with a trail entry.
    ``delete_tool_classes`` is :func:`core.response.ceiling.delete_tool_classes`.
    """
    wanted = sorted(set(names))

    def change(current: Dict[str, Any], session) -> Tuple[Dict[str, Any], str]:
        if current.get("revision") != revision or current.get("hash") != digest:
            raise SourcesRefused(
                409,
                f"The applied sources revision is now {current.get('revision')}; "
                "review it again",
            )
        sources = current.get("sources") or {}
        pending = {n for n in sources if not is_accepted(n, current)}
        if not wanted:
            raise SourcesRefused(409, "Name the sources you reviewed")
        if not_pending := [n for n in wanted if n not in pending]:
            raise SourcesRefused(
                409,
                f"Not pending: {', '.join(not_pending)}; review the sources again",
            )
        accepted = dict(current.get("accepted") or {})
        notes: List[str] = []
        doomed: List[str] = []
        for name in wanted:
            source, before = sources[name], accepted.get(name)
            if before is None:
                rows = delete_tool_classes(session, f"{name}_", user_id)
                keys = record_credential_deletes(session, name, user_id, "new source")
                doomed += keys
                notes.append(
                    f"{name} (new; {len(rows)} class row(s), {len(keys)} credential(s) cleared)"
                )
            elif before.get("url") != source["url"]:
                keys = record_credential_deletes(session, name, user_id, "url moved")
                doomed += keys
                notes.append(f"{name} (url moved; {len(keys)} credential(s) cleared)")
            else:
                notes.append(f"{name} (changed)")
            accepted[name] = {
                "hash": source["launch_hash"],
                "url": source["url"],
                "headers": source["headers"],
                "credentials": source["credentials"],
                "revision": revision,
                "by": user_id,
                "by_name": who,
                "at": utcnow().isoformat(),
            }
        reason = f"sources revision {revision}: accepted by {who}: {', '.join(notes)}"
        delete_stored(doomed)  # last, after every trail entry is in
        return {**current, "accepted": accepted, "reason": reason}, reason

    return update_applied(change, user_id)


# --- View --------------------------------------------------------------------


def _credential_state(key: str) -> Optional[str]:
    """``stored`` (encrypted store), ``environment`` (env or .env), or None."""
    from core.secrets_manager import get_secrets_manager

    manager = get_secrets_manager()
    backend = manager.encrypted_backend
    if backend.is_available() and backend.get(key):
        return "stored"
    if os.environ.get(key) or manager.get(key):
        return "environment"
    return None


def sources_view(row: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """``GET /api/config/sources``. Header values and credential values never appear."""
    row = applied_sources(fresh=True) if row is None else row
    directory = row.get("directory")
    base = {
        "directory": directory,
        "declared": bool(directory),
        "revision": row.get("revision"),
        "hash": row.get("hash"),
        "state": "none",
        "reason": NOTHING_DECLARED,
        "applied_at": row.get("applied_at"),
        "rejected": None,
        "sources": [],
    }
    if not directory:
        from core.intent import declared_dir

        if declared_dir() is not None:  # set here; the daemon has not applied yet
            base["reason"] = "Waiting for the daemon to apply the declared directory"
        return base
    base.update(
        state=row.get("state") or "none",
        reason=row.get("reason") or "",
        rejected=row.get("rejected"),
    )
    accepted = row.get("accepted") or {}
    for name, source in sorted((row.get("sources") or {}).items()):
        credentials = []
        for field in source.get("credentials") or []:
            key = credential_key(name, field)
            state = _credential_state(key)
            credentials.append(
                {"field": field, "key": key, "set": state is not None, "from": state}
            )
        mine = accepted.get(name)
        base["sources"].append(
            {
                "name": name,
                "integration_id": integration_id(name),
                "file": source.get("file"),
                "url": source.get("url"),
                "headers": sorted(source.get("headers") or {}),
                "credentials": credentials,
                "body": source.get("body") or "",
                "launch_hash": source.get("launch_hash"),
                "status": "accepted" if is_accepted(name, row) else "pending",
                "pending": pending_change(name, row),
                "accepted": (
                    {k: mine.get(k) for k in ("hash", "revision", "by_name", "at")}
                    if mine
                    else None
                ),
            }
        )
    return base
