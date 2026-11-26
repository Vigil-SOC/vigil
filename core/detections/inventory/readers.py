"""Read every rule file of one configured detection source.

A reader returns the whole list or raises: sync marks whatever a list leaves out
as removed, so a source that cannot be read must not come back short. A file
that does not parse is not a rule and is skipped, as the in-memory loader did.
"""

from __future__ import annotations

import json
import logging
import os
import re
import tomllib

# Serialises a rule wazuh_xml.parse() already read; parses nothing itself.
import xml.etree.ElementTree as ET  # nosec B405
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import yaml

from core.detections import wazuh_xml
from core.detections.gaps import TACTICS, normalize_label
from core.detections.inventory.record import RuleRecord

logger = logging.getLogger(__name__)

# The C build of SafeLoader when present: same safe subset, much faster.
_YAML_LOADER = getattr(yaml, "CSafeLoader", yaml.SafeLoader)

# ``id = "T1219"`` under [[rule.threat.technique]] and its subtechnique tables.
ELASTIC_TECHNIQUE_ID = re.compile(r'^id\s*=\s*"(T\d{4}(?:\.\d{3})?)"', re.MULTILINE)
ELASTIC_TACTIC_NAME = re.compile(
    r'^\[rule\.threat\.tactic\]\s*^id\s*=\s*"[^"]*"\s*^name\s*=\s*"([^"]+)"',
    re.MULTILINE,
)
TECHNIQUE_ID = re.compile(r"(T\d{4}(?:\.\d{3})?)")

# Kibana rule fields (the same keys as a repo toml's [rule]) that change what
# fires or how loud it is, besides the query.
ELASTIC_LOGIC_KEYS = (
    "language",
    "type",
    "index",
    "data_view_id",
    "filters",
    "severity",
    "risk_score",
    "threshold",
    "new_terms_fields",
    "history_window_start",
    "threat_query",
    "threat_mapping",
    "threat_index",
    "machine_learning_job_id",
    "anomaly_threshold",
    "from",
    "interval",
    "timestamp_override",
)

# The files each format reads; KQL repos also carry these two docs. Sigma
# rules ship as .yml and .yaml alike; a YAML file that holds no ``detection``
# or ``correlation`` is skipped by _yaml_doc, so a Sentinel .yaml beside them
# never reads as Sigma. detection_rules_service builds its map from this one.
FORMAT_SUFFIXES = {
    "sigma": (".yml", ".yaml"),
    "splunk": (".yml",),
    "elastic": (".toml",),
    "kql": (".yaml", ".md", ".kql"),
    "wazuh": (".xml",),
    "auto": (".yml", ".yaml", ".toml", ".md", ".kql", ".xml"),
}
KQL_SKIPPED_DOCS = ("readme.md", "detectiontemplate.md")


class SourceUnreadable(Exception):
    """The source could not be read in full; sync leaves its rules alone."""


def _json_safe(value: Any) -> Any:
    # YAML dates and the like, as JSONB will hold them.
    return json.loads(json.dumps(value, default=str))


def _dump(value: Any) -> str:
    try:
        return json.dumps(value, sort_keys=True, default=str)
    except TypeError:  # keys of mixed types cannot be sorted
        return json.dumps(value, default=str)


def _techniques(named: Any) -> List[str]:
    if not isinstance(named, list):
        return []
    return list(dict.fromkeys(t.upper() for t in named if isinstance(t, str)))


def _tactics(names: Any) -> List[str]:
    if not isinstance(names, list):
        return []
    # Sentinel writes PascalCase ("CommandAndControl"): split it into words first.
    return sorted(
        {
            normalize_label(re.sub(r"(?<=[a-z])(?=[A-Z])", " ", name))
            for name in names
            if isinstance(name, str)
        }
        & TACTICS
    )


def _sigma(doc: Dict[str, Any], rel: str) -> RuleRecord:
    tags = doc.get("tags") or []
    correlation = doc.get("correlation")
    logic = {"level": doc.get("level"), "logsource": doc.get("logsource")}
    if correlation is not None:
        # A correlation over other rules, not a detection of its own.
        kind = correlation.get("type") if isinstance(correlation, dict) else None
        logic["correlation"] = kind or True
    return RuleRecord(
        native_key=str(doc.get("id") or rel),
        name=str(doc.get("title") or doc.get("name") or rel),
        language="sigma",
        path=rel,
        query=_dump(doc.get("detection") if correlation is None else correlation),
        techniques=_techniques(
            [
                tag.replace("attack.", "")
                for tag in tags
                if isinstance(tag, str) and tag.startswith("attack.t")
            ]
        ),
        tactics=_tactics(
            [
                tag[len("attack.") :]
                for tag in tags
                if isinstance(tag, str) and tag.startswith("attack.")
            ]
        ),
        logic=logic,
        content=_json_safe(doc),
    )


def _splunk(doc: Dict[str, Any], rel: str) -> RuleRecord:
    # ESCU moved this to the top level; older clones keep it under tags.
    tags = doc.get("tags")
    nested = tags.get("mitre_attack_id") if isinstance(tags, dict) else None
    return RuleRecord(
        native_key=str(doc.get("id") or rel),
        name=str(doc.get("name") or doc.get("title") or rel),
        language="splunk",
        path=rel,
        query=str(doc.get("search") or ""),
        techniques=_techniques(doc.get("mitre_attack_id") or nested or []),
        logic={"type": doc.get("type")},
        content=_json_safe(doc),
    )


def _elastic(text: str, rel: str) -> RuleRecord:
    try:
        rule = tomllib.loads(text).get("rule")
    except tomllib.TOMLDecodeError:
        rule = None
    if not isinstance(rule, dict):
        # Still a rule file: fingerprint the text, key it by path.
        rule = {}
    content: Dict[str, Any] = {"_content": text}
    for key in ("name", "description"):
        if isinstance(rule.get(key), str):
            content[key] = rule[key]
    return RuleRecord(
        native_key=str(rule.get("rule_id") or rel),
        name=str(rule.get("name") or Path(rel).stem),
        language="elastic",
        path=rel,
        query=str(rule.get("query") or ("" if rule else text)),
        techniques=_techniques(ELASTIC_TECHNIQUE_ID.findall(text)),
        tactics=_tactics(ELASTIC_TACTIC_NAME.findall(text)),
        logic={k: rule.get(k) for k in ELASTIC_LOGIC_KEYS},
        content=_json_safe(content),
    )


def _wazuh_record(
    rule: ET.Element, enclosing: List[str], rel: str, content: str
) -> Optional[RuleRecord]:
    """One Wazuh rule keyed by its id, as the number the manager reads it as."""
    raw = wazuh_xml.rule_id(rule)
    if raw is None:
        return None
    number = wazuh_xml.number(raw)
    key = raw if number is None else str(number)
    level = rule.get("level")
    return RuleRecord(
        native_key=key,
        name=wazuh_xml.description(rule) or key,
        language="wazuh",
        path=rel,
        query=ET.tostring(rule, encoding="unicode").strip(),
        techniques=wazuh_xml.techniques(rule),
        logic={
            "level": int(level) if level and level.strip().isdigit() else level,
            "groups": wazuh_xml.groups(rule, enclosing),
            "if_sid": (rule.findtext("if_sid") or "").strip() or None,
        },
        content={"_content": content, "description": wazuh_xml.description(rule)},
    )


def _wazuh(text: str, rel: str) -> Optional[RuleRecord]:
    """A file holding one Wazuh rule, as written; None for anything else
    (a ruleset file of many rules, or XML that is no rule)."""
    try:
        rules = wazuh_xml.parse_all(text)
    except wazuh_xml.NotARule:
        return None
    if len(rules) != 1:
        return None
    rule, enclosing = rules[0]
    return _wazuh_record(rule, enclosing, rel, text)


def wazuh_records(text: str, rel: str) -> List[RuleRecord]:
    """Every rule in a Wazuh rule file. A file of one rule reads as _wazuh();
    each rule of a ruleset file (a local_rules.xml) as its own XML, all at
    the file's path. A decoder or other well-formed XML file holds none; text
    that is no XML raises NotARule, for read_source() to report."""
    rules = wazuh_xml.parse_all(text)
    if len(rules) == 1:
        rule, enclosing = rules[0]
        one = _wazuh_record(rule, enclosing, rel, text)
        return [one] if one else []
    found = (
        _wazuh_record(rule, enclosing, rel, ET.tostring(rule, encoding="unicode"))
        for rule, enclosing in rules
    )
    return [record for record in found if record]


def _kql_yaml(doc: Dict[str, Any], rel: str) -> RuleRecord:
    return RuleRecord(
        native_key=str(doc.get("id") or rel),
        name=str(doc.get("title") or doc.get("name") or rel),
        language="kql",
        path=rel,
        query=str(doc.get("query") or ""),
        # Sentinel analytics name them relevantTechniques; Bert-JanP, mitre.
        techniques=_techniques(doc.get("relevantTechniques") or doc.get("mitre") or []),
        tactics=_tactics(doc.get("tactics") or []),
        content=_json_safe(doc),
    )


MD_FENCE = re.compile(
    r"^[ \t]*```[ \t]*(\w*)[^\n]*\n(.*?)^[ \t]*```", re.MULTILINE | re.DOTALL
)


def _kql_markdown(text: str, rel: str) -> Optional[RuleRecord]:
    """A Hunting-Queries-Detection-Rules page: a title, a technique table, KQL blocks."""
    path = Path(rel)
    title_match = re.search(r"^#\s+(.+)", text, re.MULTILINE)
    title = title_match.group(1).strip() if title_match else path.stem
    desc_match = re.search(
        r"####\s+Description\s*\n(.*?)(?=\n####|\n##|\n```|\Z)", text, re.DOTALL
    )
    # A KQL/kusto fence, or an untagged one holding a pipeline; a page with
    # neither (a README, a changelog, notes) holds no query and is no rule.
    blocks = [
        body
        for tag, body in MD_FENCE.findall(text)
        if tag.lower() in ("kql", "kusto")
        or (not tag and re.search(r"^\s*\|", body, re.MULTILINE))
    ]
    query = "\n---\n".join(block.strip() for block in blocks)
    if not query:
        return None
    techniques = sorted(set(TECHNIQUE_ID.findall(text)))
    return RuleRecord(
        native_key=rel,
        name=title,
        language="kql",
        path=rel,
        query=query,
        techniques=_techniques(techniques),
        content={
            "title": title,
            "name": title,
            "description": desc_match.group(1).strip() if desc_match else "",
            "query": query,
            "category": path.parent.name if len(path.parts) > 1 else "General",
            "mitre": techniques,
        },
    )


def _kql_file(text: str, rel: str) -> Optional[RuleRecord]:
    if not text.strip():
        return None
    return RuleRecord(
        native_key=rel,
        name=Path(rel).stem,
        language="kql",
        path=rel,
        query=text,
        techniques=_techniques(sorted(set(TECHNIQUE_ID.findall(text)))),
        content={"query": text},
    )


def _deep_merge(base: Dict[str, Any], over: Dict[str, Any]) -> Dict[str, Any]:
    merged = dict(base)
    for key, value in over.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def _yaml_doc(fmt: str, doc: Dict[str, Any], rel: str) -> Optional[RuleRecord]:
    """The rule one YAML document holds, or None when it holds no query."""
    if fmt == "auto":
        if "detection" in doc or "correlation" in doc:
            fmt = "sigma"
        elif "search" in doc:
            fmt = "splunk"
        elif "query" in doc:
            fmt = "kql"
        else:
            return None
    if fmt == "sigma" and "detection" not in doc and "correlation" not in doc:
        return None
    if fmt == "splunk" and "search" not in doc:
        return None
    if fmt == "kql" and "query" not in doc:
        return None
    parsers: Dict[str, Callable[[Dict[str, Any], str], RuleRecord]] = {
        "sigma": _sigma,
        "splunk": _splunk,
        "kql": _kql_yaml,
    }
    return parsers[fmt](doc, rel)


def parse_file(fmt: str, path: Path, rel: str, text: str) -> List[RuleRecord]:
    """The rules a file holds under ``fmt``: one per YAML document, at most one
    for any other file (a Wazuh ruleset file reads through wazuh_records())."""
    suffix = path.suffix
    if suffix == ".toml":
        return [_elastic(text, rel)]
    if suffix == ".md":
        if path.name.lower() in KQL_SKIPPED_DOCS:
            return []
        one = _kql_markdown(text, rel)
    elif suffix == ".kql":
        one = _kql_file(text, rel)
    elif suffix == ".xml":
        one = _wazuh(text, rel)
    else:
        return _yaml_records(fmt, text, rel)
    return [one] if one else []


def _yaml_records(fmt: str, text: str, rel: str) -> List[RuleRecord]:
    docs = list(yaml.load_all(text, Loader=_YAML_LOADER))  # nosec B506
    if len(docs) == 1:
        one = _yaml_doc(fmt, docs[0], rel) if isinstance(docs[0], dict) else None
        return [one] if one else []
    # A Sigma collection: an ``action: global`` document is merged into the
    # documents after it; each other document is its own rule.
    records: List[RuleRecord] = []
    base: Dict[str, Any] = {}
    for n, doc in enumerate(docs, start=1):
        if not isinstance(doc, dict):
            continue
        action = doc.get("action")
        own = {k: v for k, v in doc.items() if k != "action"}
        if action == "global":
            base = _deep_merge(base, own)
            continue
        if action == "reset":
            base = {}
            continue
        record = _yaml_doc(fmt, _deep_merge(base, own), rel)
        if record:
            # Keyed by its own id: one inherited from the global document is shared.
            record.native_key = str(own.get("id") or f"{rel}#{n}")
            records.append(record)
    return records


def _raise(error: OSError) -> None:
    raise error


def _files(root: Path, suffixes: tuple) -> List[Path]:
    # os.walk, not rglob: rglob skips a directory it cannot open, which would
    # read as every rule under it deleted.
    found: List[Path] = []
    for directory, subdirs, files in os.walk(root, onerror=_raise):
        subdirs[:] = [d for d in subdirs if d != ".git"]
        found.extend(Path(directory, f) for f in files if Path(f).suffix in suffixes)
    return sorted(found)


# Vigil's own source: the rules it drafts, as plain files the customer owns.
OVERLAY_ID = "overlay"


def source_name(source: Dict[str, Any]) -> str:
    return f"repo:{source['id']}"


OVERLAY_SOURCE = source_name({"id": OVERLAY_ID})


def read_source(source: Dict[str, Any], root: Path) -> List[RuleRecord]:
    """Every rule under ``root`` (the source's rules directory), keys unique."""
    if not root.is_dir():
        raise SourceUnreadable(f"{root} is not a directory")
    fmt = source.get("format") or "auto"
    records: List[RuleRecord] = []
    keys: set = set()
    for path in rule_files(root, fmt):
        rel = path.relative_to(root).as_posix()
        # An OSError is not a bad file: it fails the whole source.
        raw = path.read_bytes()
        try:
            found = read_file(fmt, path, rel, raw)
        except Exception as exc:  # noqa: BLE001 -- a malformed file is not a rule
            logger.warning(
                "detection rules: %s not read, no rules listed: %s", rel, exc
            )
            found = []
        for record in found:
            # Two rules with one id stay two rows: the first by path keeps the id.
            if record.native_key in keys:
                logger.warning(
                    "detection rules: %s repeats id %s; keyed by path",
                    rel,
                    record.native_key,
                )
                key, n = f"{record.native_key}@{rel}", 1
                # A file of many rules can repeat one id more than once.
                while key in keys:
                    n += 1
                    key = f"{record.native_key}@{rel}#{n}"
                record.native_key = key
            keys.add(record.native_key)
            records.append(record)
    return records


def rule_files(root: Path, fmt: str) -> List[Path]:
    """The files under ``root`` a source of ``fmt`` reads, sorted; .git skipped."""
    return _files(root, FORMAT_SUFFIXES.get(fmt, FORMAT_SUFFIXES["auto"]))


def read_file(fmt: str, path: Path, rel: str, raw: bytes) -> List[RuleRecord]:
    """The rules one file's bytes hold; raises when the file does not parse."""
    # Postgres text cannot hold NUL, so such a file is not stored.
    return [] if b"\x00" in raw else _records(fmt, path, rel, raw)


def _records(fmt: str, path: Path, rel: str, raw: bytes) -> List[RuleRecord]:
    """The rules a file holds: many in a Wazuh ruleset file or a YAML collection."""
    text = raw.decode("utf-8")
    if path.suffix == ".xml":
        return wazuh_records(text, rel)
    return parse_file(fmt, path, rel, text)
