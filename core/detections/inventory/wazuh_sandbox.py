"""Count a Wazuh copy in shadow with Wazuh's own engine: Vigil's sandbox (#4099).

A copy that matches the raw log line (``<match>``, ``<regex>``, ``<pcre2>``), or
uses anything else ``wazuh_shadow`` can't turn into an archive query exactly
(``if_sid`` parents, ``<hostname>``, negation, osregex...), is counted by
replaying archived events through ``wazuh-logtest`` on a Wazuh manager Vigil
runs for itself: the ``wazuh-sandbox`` compose profile, pinned to the Wazuh
release Vigil reads (4.9.2). Nothing is written to the customer's manager, and
it is never asked to replay anything.

Why a sandbox and not the customer's manager: logtest matches only the rules on
the manager's disk, and a shadow copy is not there (checked on 4.9.2; the
request has no field for a rule). A logtest session reads the ruleset when it
opens, so files uploaded over the server API (``PUT /rules/files``,
``PUT /decoders/files``) count from the next session, with no restart.

Each run loads the sandbox with:

- every rule and decoder file in the customer's repositories (the clones Vigil
  syncs), as written, except a rule a shadow copy replaces (same id);
- the overlay's Wazuh copies, shadow and live.

Wazuh's stock ruleset is the image's own. A rule or decoder only on the
customer's manager is not in the sandbox: a copy whose ``if_sid``,
``if_group`` or ``decoded_as`` names one reads "can't validate: not in repo".

Then it takes a random sample of up to ``SAMPLE_EVENTS`` archived events in the
copies' window that keep their raw line (``full_log``), replays each through one
logtest session, and counts the events whose final rule (logtest reports one,
the deepest that matched, a level-0 rule included) is the copy. An event the
sandbox decodes with another decoder than the customer's manager did is left out
of the count, and the run says how many. So a count is hits in the sample, not
in the window, and the run records how big the sample was.

Rules that need what a sample breaks are not counted: ``frequency`` /
``timeframe``, ``ignore``, ``if_matched_*`` and ``same_*`` / ``different_*``
(each needs the events before it, in order), and ``<list>`` (CDB lists are not
loaded).
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import time
import xml.etree.ElementTree as ET  # nosec B405 -- serialises rules already parsed
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple
from urllib.parse import urlparse

from core.detections import wazuh_xml
from core.detections.inventory.wazuh_shadow import CANT
from core.integrations.wazuh.client import WazuhError, WazuhService

logger = logging.getLogger(__name__)

ENGINE = "Vigil's Wazuh sandbox (wazuh-logtest 4.9.2)"
FILE_PREFIX = "vigil_sandbox_"
# Events replayed per run, at most; one logtest call each (~60 ms on 4.9.2).
SAMPLE_EVENTS = 500
# The sandbox's API answers 429 past 300 calls a minute (its default, kept so
# the image runs unmodified): every call is paced under it, uploads and checks
# as well as logtest. A run's 500 events take about two minutes.
CALLS_PER_MINUTE = 240
# A run waits until the copies' window is this long: at most 4 runs, and so
# 4 x SAMPLE_EVENTS replays, a day.
EVERY = timedelta(hours=6)
NOT_IN_REPO = f"{CANT}not in repo: "
NOT_A_SANDBOX = f"{CANT}the Wazuh sandbox "
FAILED = f"{CANT}Wazuh sandbox: "
NO_SANDBOX = (
    "no Wazuh sandbox is configured to count it (the wazuh-sandbox compose profile)"
)

_CORRELATED = re.compile(r"if_matched_\w+|(same|different)_\w+")
_RULE = re.compile(r"<rule\b")
_DECODER = re.compile(r"<decoder\b")
_IDS = re.compile(r"[\s,]+")


def configured() -> Optional[Dict[str, str]]:
    """The sandbox's URL and login, or None when none is configured."""
    from core.config import get_settings

    settings = get_settings()
    url = (settings.wazuh_sandbox_url or "").strip()
    if not url:
        return None
    password = settings.wazuh_sandbox_password
    return {
        "url": url,
        "username": settings.wazuh_sandbox_username,
        "password": password.get_secret_value() if password else "",
    }


def customer() -> Optional[Dict[str, Any]]:
    """The customer's Wazuh integration (its resolved config), or None."""
    from core.config import load_integrations_config
    from core.integrations._base.config import missing, resolve
    from core.integrations.wazuh.descriptor import WAZUH, WAZUH_REQUIRED

    if WAZUH.id not in load_integrations_config()["enabled_integrations"]:
        return None
    config = resolve(WAZUH)
    return None if missing(config, *WAZUH_REQUIRED) else dict(config)


async def customer_hostname(config: Mapping[str, Any]) -> Optional[str]:
    """The customer's manager's hostname (``GET /``), or None when unread."""
    verify = True if config.get("verify_ssl") is None else config["verify_ssl"]
    service = WazuhService(
        config["api_url"],
        config["username"],
        config["password"],
        verify_ssl=verify,
        ca_cert_path=config.get("ca_cert_path"),
    )
    try:
        return (await service._request("GET", "/")).get("hostname")
    except WazuhError:
        return None
    finally:
        await service.close()


def _endpoint(url: str) -> Tuple[str, int]:
    parts = urlparse(url if "://" in url else f"https://{url}")
    return (parts.hostname or "").lower(), parts.port or 55000


def refusal(text: str) -> Optional[str]:
    """Why the sandbox can't count this rule either, or None."""
    try:
        rule = wazuh_xml.parse(text)
    except wazuh_xml.NotARule as exc:
        return f"{CANT}{exc}"
    reasons: List[str] = []
    for attr in ("frequency", "timeframe"):
        if rule.get(attr) is not None:
            reasons.append(f"{attr} needs every event before it, not a sample")
    if rule.get("ignore") is not None:
        reasons.append("ignore suppresses by time after an earlier match")
    for node in rule:
        if _CORRELATED.fullmatch(node.tag):
            reasons.append(f"<{node.tag}> needs the events before it, not a sample")
        elif node.tag == "list":
            reasons.append("<list> lookups aren't loaded in the sandbox")
    return CANT + "; ".join(dict.fromkeys(reasons)) if reasons else None


def parents(text: str) -> Dict[str, List[str]]:
    """What the rule needs loaded beside it: the rules (``if_sid``), groups
    (``if_group``) and decoders (``decoded_as``) it names."""
    rule = wazuh_xml.parse(text)
    found: Dict[str, List[str]] = {"if_sid": [], "if_group": [], "decoded_as": []}
    for node in rule:
        value = (node.text or "").strip()
        if node.tag in found and value:
            found[node.tag] += _IDS.split(value) if node.tag == "if_sid" else [value]
    return found


@dataclass
class Ruleset:
    rules: Dict[str, str] = field(default_factory=dict)  # file name -> XML
    decoders: Dict[str, str] = field(default_factory=dict)
    skipped: List[str] = field(default_factory=list)  # repo files not loaded, why


def _xml_files(root: Path) -> List[Path]:
    found: List[Path] = []
    for directory, subdirs, files in os.walk(root):
        subdirs[:] = sorted(d for d in subdirs if d != ".git")
        found.extend(Path(directory, f) for f in sorted(files) if f.endswith(".xml"))
    return found


def _without(text: str, ids: Iterable[str]) -> Optional[str]:
    """``text`` without the rules whose id is in ``ids``, each kept rule in its
    enclosing groups; ``text`` itself when none is; None when it can't be read
    (the manager reads more than ElementTree does, so such a file goes as is)."""
    try:
        rules = wazuh_xml.parse_all(text)
    except wazuh_xml.NotARule:
        return None
    drop = set(ids)
    if not any(wazuh_xml.rule_id(r) in drop for r, _ in rules):
        return text
    kept = [
        f'<group name="{",".join(n for n in names if n) or "local,"}">'
        f"{ET.tostring(rule, encoding='unicode')}</group>"
        for rule, names in rules
        if wazuh_xml.rule_id(rule) not in drop
    ]
    return "\n".join(kept)


def ruleset(copies: Sequence[Any], roots: Sequence[Path]) -> Ruleset:
    """What the sandbox loads: the repositories' rule and decoder files and the
    overlay's copies (``key``, ``toml``), named ``vigil_sandbox_<n>.xml``."""
    out = Ruleset()
    keys = {c.key for c in copies}
    n = 0

    def add(kind: Dict[str, str], text: str) -> None:
        nonlocal n
        kind[f"{FILE_PREFIX}{n}.xml"] = text
        n += 1

    for root in roots:
        for path in _xml_files(root):
            try:
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError) as exc:
                out.skipped.append(f"{path.name}: {type(exc).__name__}")
                continue
            if _RULE.search(text):
                # The copy is the newer text of a rule it shares an id with.
                kept = _without(text, keys)
                add(out.rules, text if kept is None else kept)
            elif _DECODER.search(text):
                add(out.decoders, text)
    for copy in copies:
        add(out.rules, copy.toml)
    return out


def repo_roots() -> List[Path]:
    """The rules directory of each repository source that is ready, but the
    overlay's own (its copies come from the inventory)."""
    from core.detections.detection_rules_service import DetectionRulesService
    from core.detections.inventory.readers import OVERLAY_ID

    service = DetectionRulesService()
    roots = []
    for source in service.list_sources():
        if source.get("id") == OVERLAY_ID or source.get("status") != "ready":
            continue
        try:
            root = Path(
                service._under(source["local_path"], source.get("subdirectory", ""))
            )
        except (KeyError, ValueError):
            continue
        if root.is_dir():
            roots.append(root)
    return roots


class Sandbox(WazuhService):
    """Vigil's own Wazuh manager. Unlike the customer's (``WazuhService``,
    read-only), Vigil writes its rule and decoder files."""

    pace = 60.0 / CALLS_PER_MINUTE  # seconds between calls
    _next = 0.0  # monotonic time the next call may go

    async def _request(self, method: str, path: str, **kwargs) -> Dict[str, Any]:
        wait = self._next - time.monotonic()
        if wait > 0:
            await asyncio.sleep(wait)
        self._next = time.monotonic() + self.pace
        return await super()._request(method, path, **kwargs)

    async def guard(self, theirs: Optional[Mapping[str, Any]]) -> Optional[str]:
        """Why this is not a sandbox Vigil may write to, or None: it is the
        customer's manager (the Wazuh integration's host and port, or the same
        manager hostname), or it has agents, which no sandbox does."""
        if theirs and _endpoint(str(theirs["api_url"])) == _endpoint(self.api_url):
            return "is the customer's Wazuh manager (the same host and port)"
        data = await self._request(
            "GET", "/agents", params={"limit": 2, "select": "id"}
        )
        ids = [str(a.get("id")) for a in data.get("affected_items") or []]
        if any(i != "000" for i in ids):
            return "has agents reporting to it, so it is a real manager, not a sandbox"
        if theirs:
            ours = (await self._request("GET", "/")).get("hostname")
            if ours and ours == await customer_hostname(theirs):
                return "is the customer's Wazuh manager (the same manager hostname)"
        return None

    async def _files(self, kind: str) -> List[str]:
        data = await self._request(
            "GET", f"/{kind}/files", params={"search": FILE_PREFIX, "limit": 500}
        )
        names = [i.get("filename", "") for i in data.get("affected_items") or []]
        return [n for n in names if n.startswith(FILE_PREFIX)]

    async def load(self, rs: Ruleset) -> None:
        """Replace the sandbox's ``vigil_sandbox_*`` files with ``rs``."""
        for kind, files in (("decoders", rs.decoders), ("rules", rs.rules)):
            for name, text in files.items():
                await self._request(
                    "PUT",
                    f"/{kind}/files/{name}",
                    params={"overwrite": "true"},
                    content=text.encode("utf-8"),
                    headers={"Content-Type": "application/octet-stream"},
                )
            for name in set(await self._files(kind)) - set(files):
                await self._request("DELETE", f"/{kind}/files/{name}")

    async def missing(self, needs: Mapping[str, Iterable[str]]) -> List[str]:
        """Of the rules, groups and decoders named, those the sandbox lacks."""
        absent: List[str] = []
        sids = sorted({s for s in needs.get("if_sid", ()) if s.isdigit()})
        if sids:
            try:
                have, _ = await self.list_rules(rule_ids=sids)
            except WazuhError as exc:
                # When none of the ids is loaded the manager fails the whole call.
                if "does not exist" not in str(exc):
                    raise
                have = []
            loaded = {str(r.get("id")) for r in have}
            absent += [f"if_sid {s}" for s in sids if s not in loaded]
        for group in sorted(set(needs.get("if_group", ()))):
            data = await self._request(
                "GET", "/rules", params={"group": group, "limit": 1}
            )
            if not data.get("total_affected_items"):
                absent.append(f"if_group {group}")
        for name in sorted(set(needs.get("decoded_as", ()))):
            data = await self._request(
                "GET", "/decoders", params={"decoder_names": name, "limit": 1}
            )
            if not data.get("total_affected_items"):
                absent.append(f"decoded_as {name}")
        return absent

    async def replay(
        self, events: Sequence[Mapping[str, str]]
    ) -> List[Optional[Dict[str, Any]]]:
        """Each event's logtest ``output`` (None when logtest refused it), all in
        one session, opened after the last load and closed afterwards."""
        token: Optional[str] = None
        outputs: List[Optional[Dict[str, Any]]] = []
        try:
            for event in events:
                body = {**event, **({"token": token} if token else {})}
                try:
                    data = await self._request("PUT", "/logtest", json=body)
                except WazuhError as exc:
                    token = token or exc.data.get("token")
                    outputs.append(None)
                    continue
                token = token or data.get("token")
                output = data.get("output")
                outputs.append(output if isinstance(output, dict) else None)
        finally:
            if token:
                try:
                    await self._request("DELETE", f"/logtest/sessions/{token}")
                except WazuhError as exc:
                    logger.warning("Wazuh sandbox session not closed: %s", exc)
        return outputs


def event(doc: Mapping[str, Any]) -> Optional[Dict[str, str]]:
    """An archived event as logtest takes it; None without its raw line."""
    source = doc.get("_source") or {}
    line = source.get("full_log")
    if not isinstance(line, str) or not line:
        return None
    decoder = (source.get("decoder") or {}).get("name")
    location = str(source.get("location") or "")
    if location.lower() == "eventchannel":
        log_format = "eventchannel"
    elif decoder == "json":
        log_format = "json"
    else:
        # The archive keeps no log_format: a localfile read as another format
        # (audit, multi-line...) may decode differently, and is left out.
        log_format = "syslog"
    return {
        "event": line,
        "log_format": log_format,
        "location": str(source.get("location") or "vigil-sandbox"),
    }


def sample_query(seed: int) -> Dict[str, Any]:
    """A random, repeatable-per-seed pick of archived events that keep their line."""
    return {
        "function_score": {
            "query": {"exists": {"field": "full_log"}},
            "random_score": {"seed": seed, "field": "_seq_no"},
            "boost_mode": "replace",
        }
    }


@dataclass
class Tally:
    """One sandbox run: hits and their events per copy key, and the sample."""

    hits: Dict[str, List[Dict[str, Any]]] = field(default_factory=dict)
    replayed: int = 0
    window_events: int = 0
    decoded_differently: int = 0
    refused: int = 0

    def sampled(self) -> Dict[str, Any]:
        return {
            "engine": ENGINE,
            "events": self.replayed,
            "of": self.window_events,
            "decoded_differently": self.decoded_differently,
            "refused": self.refused,
        }


def count(
    docs: Sequence[Mapping[str, Any]],
    outputs: Sequence[Optional[Mapping[str, Any]]],
    starts: Mapping[str, datetime],
    window_events: int,
) -> Tally:
    """Hits per copy key: events at or after the copy's window start whose
    final rule is the copy, among those the sandbox decoded as the manager did."""
    tally = Tally(window_events=window_events)
    for doc, output in zip(docs, outputs):
        if output is None:
            tally.refused += 1
            continue
        source = doc.get("_source") or {}
        theirs = (source.get("decoder") or {}).get("name")
        ours = (output.get("decoder") or {}).get("name")
        if theirs and ours != theirs:
            tally.decoded_differently += 1
            continue
        tally.replayed += 1
        rule = str((output.get("rule") or {}).get("id") or "")
        start = starts.get(rule)
        if start is None:
            continue
        at = _timestamp(source.get("@timestamp"))
        if at is None or at >= start:
            tally.hits.setdefault(rule, []).append(dict(doc))
    return tally


def _timestamp(value: Any) -> Optional[datetime]:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def due(windows: Iterable[Tuple[datetime, datetime, bool]]) -> bool:
    """Whether a sandbox run is due: a copy never run, or the copies' window,
    from the earliest start, at least ``EVERY`` long."""
    spans = list(windows)
    if not spans:
        return False
    if any(never for _, _, never in spans):
        return True
    return max(end for _, end, _ in spans) - min(s for s, _, _ in spans) >= EVERY
