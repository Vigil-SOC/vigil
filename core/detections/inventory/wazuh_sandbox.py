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

Each run clears the files it loaded last time, then loads:

- each copy it counts, in a file of its own (wrapped in a ``<group>``);
- from the customer's repositories (the clones Vigil syncs) and the overlay's
  other copies, only the files those copies need: the rules their ``if_sid`` /
  ``if_group`` name and the decoders their ``decoded_as`` names, and those
  files' own parents, that Wazuh's stock ruleset (the image's own) lacks. A
  repository carrying the whole stock ruleset would otherwise double it.

A file the manager refuses is deleted again at once (the manager keeps it, and
every later upload would fail) and reported; the rest still load. A copy whose
own file is refused reads "can't validate" with the manager's reason. A rule or decoder only on the
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
REFUSED = f"{CANT}the Wazuh sandbox refused it: "
FAILED = f"{CANT}Wazuh sandbox: "
NO_SANDBOX = (
    "no Wazuh sandbox is configured to count it (the wazuh-sandbox compose profile)"
)
NO_PASSWORD = "the Wazuh sandbox has no password (set WAZUH_SANDBOX_PASSWORD)"

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


async def customer_hostname(
    config: Mapping[str, Any],
) -> Tuple[Optional[str], Optional[str]]:
    """(the customer's manager's hostname from ``GET /``, or None; why not)."""
    verify = True if config.get("verify_ssl") is None else config["verify_ssl"]
    service = WazuhService(
        config["api_url"],
        config["username"],
        config["password"],
        verify_ssl=verify,
        ca_cert_path=config.get("ca_cert_path"),
    )
    try:
        name = (await service._request("GET", "/")).get("hostname")
        return (name, None) if name else (None, "it reported no hostname")
    except WazuhError as exc:
        return None, str(exc)
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


def _needs() -> Dict[str, List[str]]:
    return {"if_sid": [], "if_group": [], "decoded_as": []}


def _parents_of(rule: ET.Element, found: Dict[str, List[str]]) -> Dict[str, List[str]]:
    for node in rule:
        value = (node.text or "").strip()
        if node.tag in found and value:
            found[node.tag] += _IDS.split(value) if node.tag == "if_sid" else [value]
    return found


def parents(text: str) -> Dict[str, List[str]]:
    """What the rule needs loaded beside it: the rules (``if_sid``), groups
    (``if_group``) and decoders (``decoded_as``) it names."""
    return _parents_of(wazuh_xml.parse(text), _needs())


def wrapped(text: str) -> str:
    """A copy as a rule file the manager loads: inside a ``<group>``."""
    if wazuh_xml.grouped(text):
        return text
    return f'<group name="vigil,">{wazuh_xml.without_declaration(text)}</group>'


@dataclass
class Repo:
    """The customer's repository Wazuh files, by what each defines and needs.
    The sandbox loads only the files the copies it counts need, never all."""

    rules: Dict[str, str] = field(default_factory=dict)  # file -> text
    decoders: Dict[str, str] = field(default_factory=dict)
    rule_file: Dict[str, str] = field(default_factory=dict)  # rule id -> file
    # group -> (rule id, file) for each rule in it
    group_rules: Dict[str, List[Tuple[str, str]]] = field(default_factory=dict)
    decoder_file: Dict[str, str] = field(default_factory=dict)  # name -> file
    needs: Dict[str, Dict[str, List[str]]] = field(default_factory=dict)
    unreadable: List[str] = field(default_factory=list)  # "file: why"

    def add_rules(self, name: str, text: str) -> None:
        rules = wazuh_xml.parse_all(text)  # NotARule: the caller's to report
        self.rules[name] = text
        needs = self.needs.setdefault(name, _needs())
        for rule, enclosing in rules:
            rid = wazuh_xml.rule_id(rule)
            if rid:
                self.rule_file.setdefault(rid, name)
            for group in wazuh_xml.groups(rule, enclosing):
                if rid:
                    self.group_rules.setdefault(group, []).append((rid, name))
            _parents_of(rule, needs)

    def add_decoders(self, name: str, text: str) -> None:
        root = wazuh_xml._root(text)  # NotARule: the caller's to report
        self.decoders[name] = text
        needs = self.needs.setdefault(name, _needs())
        for decoder in root.iter("decoder"):
            if decoder.get("name"):
                self.decoder_file.setdefault(str(decoder.get("name")), name)
            parent = (decoder.findtext("parent") or "").strip()
            if parent:
                needs["decoded_as"].append(parent)


def _xml_files(root: Path) -> List[Path]:
    found: List[Path] = []
    for directory, subdirs, files in os.walk(root):
        subdirs[:] = sorted(d for d in subdirs if d != ".git")
        found.extend(Path(directory, f) for f in sorted(files) if f.endswith(".xml"))
    return found


def read_repos(roots: Sequence[Path], others: Sequence[Any] = ()) -> Repo:
    """The repositories' rule and decoder files, and the overlay's other copies
    (live ones a counted copy may name as a parent). A file that can't be read
    is listed, never loaded: one bad file must not stop the rest."""
    repo = Repo()
    for root in roots:
        for path in _xml_files(root):
            name = f"{root.name}/{path.relative_to(root).as_posix()}"
            try:
                text = path.read_text(encoding="utf-8")
                if _RULE.search(text):
                    repo.add_rules(name, text)
                elif _DECODER.search(text):
                    repo.add_decoders(name, text)
            except (OSError, UnicodeDecodeError, wazuh_xml.NotARule) as exc:
                repo.unreadable.append(f"{name}: {exc}")
    for copy in others:
        try:
            repo.add_rules(f"overlay/{copy.key}.xml", wrapped(copy.toml))
        except wazuh_xml.NotARule as exc:
            repo.unreadable.append(f"overlay/{copy.key}.xml: {exc}")
    return repo


def _without(text: str, ids: Iterable[str]) -> str:
    """``text`` without the rules whose id is in ``ids`` (a copy is the newer
    text of a rule it shares an id with), each kept rule in its groups."""
    rules = wazuh_xml.parse_all(text)
    drop = set(ids)
    if not any(wazuh_xml.rule_id(r) in drop for r, _ in rules):
        return text
    return "\n".join(
        f'<group name="{",".join(n for n in names if n) or "local,"}">'
        f"{ET.tostring(rule, encoding='unicode')}</group>"
        for rule, names in rules
        if wazuh_xml.rule_id(rule) not in drop
    )


@dataclass
class Loaded:
    """What one load put in the sandbox, and what it couldn't."""

    refused: Dict[str, str] = field(default_factory=dict)  # copy key -> why
    skipped: List[str] = field(default_factory=list)  # "file: why"
    files: int = 0


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
    # A busy manager (a fresh one's first minutes, a big listing) answers 500
    # "Timeout executing API request": wait and ask again, this long each time.
    retry_waits: Tuple[float, ...] = (10.0, 30.0)
    _next = 0.0  # monotonic time the next call may go

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        kwargs.setdefault("timeout", 120.0)  # GET /rules takes seconds on 4.9.2
        super().__init__(*args, **kwargs)

    async def _request(self, method: str, path: str, **kwargs) -> Dict[str, Any]:
        for wait_after in (*self.retry_waits, None):
            wait = self._next - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
            self._next = time.monotonic() + self.pace
            try:
                return await super()._request(method, path, **kwargs)
            except WazuhError as exc:
                if wait_after is None or "Timeout executing API request" not in str(
                    exc
                ):
                    raise
                await asyncio.sleep(wait_after)
        raise AssertionError("unreachable")  # pragma: no cover

    async def guard(self, theirs: Optional[Mapping[str, Any]]) -> Optional[str]:
        """Why Vigil may not write to this sandbox, or None: it is the customer's
        manager (the Wazuh integration's host and port, or the same manager
        hostname), it has agents (no sandbox does), or the customer's manager
        couldn't be read to tell them apart: unconfirmed is never written to."""
        if theirs and _endpoint(str(theirs["api_url"])) == _endpoint(self.api_url):
            return "the Wazuh sandbox is your Wazuh manager (the same host and port)"
        data = await self._request(
            "GET", "/agents", params={"limit": 2, "select": "id"}
        )
        ids = [str(a.get("id")) for a in data.get("affected_items") or []]
        if any(i != "000" for i in ids):
            return (
                "the Wazuh sandbox has agents reporting to it, so it is a real "
                "manager, not a sandbox"
            )
        if theirs:
            ours = (await self._request("GET", "/")).get("hostname")
            name, why = await customer_hostname(theirs)
            if name is None:
                # Fail closed: unconfirmed is never written to.
                return (
                    "couldn't confirm the sandbox isn't your Wazuh manager (your "
                    f"manager's hostname couldn't be read: {why})"
                )
            if ours and ours == name:
                return "the Wazuh sandbox is your Wazuh manager (the same hostname)"
        return None

    async def _files(self, kind: str) -> List[str]:
        data = await self._request(
            "GET", f"/{kind}/files", params={"search": FILE_PREFIX, "limit": 500}
        )
        names = [i.get("filename", "") for i in data.get("affected_items") or []]
        return [n for n in names if n.startswith(FILE_PREFIX)]

    async def _count(self, path: str, params: Dict[str, Any]) -> int:
        """How many items a listing finds; 0 where the manager answers that
        none exists by failing the call."""
        try:
            data = await self._request("GET", path, params={**params, "limit": 1})
        except WazuhError as exc:
            if "does not exist" not in str(exc):
                raise
            return 0
        return int(data.get("total_affected_items") or 0)

    async def _loaded(self, ids: Sequence[str]) -> set:
        """Which of the rule ids the sandbox has loaded: a few listings, not one each."""
        found: set = set()
        unique = sorted(set(ids))
        for n in range(0, len(unique), 100):
            chunk = unique[n : n + 100]
            try:
                have, _ = await self.list_rules(rule_ids=chunk)
            except WazuhError as exc:
                # When none of the ids is loaded the manager fails the whole call.
                if "does not exist" not in str(exc):
                    raise
                continue
            found |= {str(r.get("id")) for r in have}
        return found

    async def _has(self, kind: str, name: str) -> bool:
        """Whether the sandbox has loaded the rule id or decoder ``name``."""
        if kind == "rule":
            return bool(await self._count("/rules", {"rule_ids": name}))
        return bool(await self._count("/decoders", {"decoder_names": name}))

    async def plan(
        self, copies: Sequence[Any], repo: Repo
    ) -> Tuple[List[str], List[str]]:
        """The repository rule and decoder files the copies need: their parents'
        files, and those files' parents, that Wazuh's stock ruleset (the
        image's) does not already hold. Only these are loaded: a repository
        that carries the whole ruleset would otherwise double the manager's."""
        keys = {c.key for c in copies}
        rules: List[str] = []
        decoders: List[str] = []
        queue = [parents(c.toml) for c in copies]
        seen: set = set()
        while queue:
            needs = queue.pop()
            for sid in needs.get("if_sid", ()):
                if sid in keys or ("sid", sid) in seen:
                    continue
                seen.add(("sid", sid))
                name = repo.rule_file.get(sid)
                if name and name not in rules and not await self._has("rule", sid):
                    rules.append(name)
                    queue.append(repo.needs[name])
            for group in needs.get("if_group", ()):
                if ("group", group) in seen:
                    continue
                seen.add(("group", group))
                # As for if_sid: only the group's rules the stock ruleset lacks.
                members = [
                    (rid, name)
                    for rid, name in repo.group_rules.get(group, ())
                    if rid not in keys and name not in rules
                ]
                stock = await self._loaded([rid for rid, _ in members])
                for rid, name in members:
                    if rid not in stock and name not in rules:
                        rules.append(name)
                        queue.append(repo.needs[name])
            for decoder in needs.get("decoded_as", ()):
                if ("decoder", decoder) in seen:
                    continue
                seen.add(("decoder", decoder))
                name = repo.decoder_file.get(decoder)
                if (
                    name
                    and name not in decoders
                    and not await self._has("decoder", decoder)
                ):
                    decoders.append(name)
                    queue.append(repo.needs[name])
        return rules, decoders

    async def _put(self, kind: str, name: str, text: str) -> Optional[str]:
        """Upload one file; on a refusal delete it again (the manager keeps a
        refused file, and every later upload then fails) and say why."""
        try:
            await self._request(
                "PUT",
                f"/{kind}/files/{name}",
                params={"overwrite": "true"},
                content=text.encode("utf-8"),
                headers={"Content-Type": "application/octet-stream"},
            )
            return None
        except WazuhError as exc:
            try:
                await self._request("DELETE", f"/{kind}/files/{name}")
            except WazuhError as gone:
                logger.warning("Wazuh sandbox kept refused %s: %s", name, gone)
            return str(exc)

    async def load(self, copies: Sequence[Any], repo: Repo) -> Loaded:
        """Clear the sandbox's ``vigil_sandbox_*`` files, then load the copies,
        each in its own file, and the repository files they need. A file the
        manager refuses is deleted and reported; the rest still load."""
        for kind in ("rules", "decoders"):
            for name in await self._files(kind):
                await self._request("DELETE", f"/{kind}/files/{name}")
        rule_files, decoder_files = await self.plan(copies, repo)
        out = Loaded(skipped=list(repo.unreadable))
        keys = [c.key for c in copies]
        uploads = [("decoders", f, repo.decoders[f], None) for f in decoder_files]
        uploads += [
            ("rules", f, _without(repo.rules[f], keys), None) for f in rule_files
        ]
        uploads += [
            ("rules", f"overlay/{c.key}", wrapped(c.toml), c.key) for c in copies
        ]
        for n, (kind, source, text, key) in enumerate(uploads):
            why = await self._put(kind, f"{FILE_PREFIX}{n}.xml", text)
            if why is None:
                out.files += 1
            elif key is not None:
                out.refused[key] = why
            else:
                out.skipped.append(f"{source}: {why}")
        return out

    async def missing(self, needs: Mapping[str, Iterable[str]]) -> List[str]:
        """Of the rules, groups and decoders named, those the sandbox lacks."""
        absent: List[str] = []
        for sid in sorted({s for s in needs.get("if_sid", ()) if s.isdigit()}):
            if not await self._has("rule", sid):
                absent.append(f"if_sid {sid}")
        for group in sorted(set(needs.get("if_group", ()))):
            if not await self._count("/rules", {"group": group}):
                absent.append(f"if_group {group}")
        for name in sorted(set(needs.get("decoded_as", ()))):
            if not await self._has("decoder", name):
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

    def estimate(self, key: str) -> int:
        """The copy's hits scaled from the sample to every archived event in the
        window, rounded up: what the rollout's per-day hit limit compares, so a
        loud rule can't look quiet because only a sample was replayed."""
        hits = len(self.hits.get(key, ()))
        if not hits or not self.replayed:
            return 0
        return -(-hits * max(self.window_events, self.replayed) // self.replayed)

    def sampled(self, key: str) -> Dict[str, Any]:
        return {
            "engine": ENGINE,
            "events": self.replayed,
            "of": self.window_events,
            "decoded_differently": self.decoded_differently,
            "refused": self.refused,
            "hits": len(self.hits.get(key, ())),
            "estimated": self.estimate(key),
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
