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
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import yaml

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

# The files each format reads; KQL repos also carry these two docs.
FORMAT_SUFFIXES = {
    "sigma": (".yml",),
    "splunk": (".yml",),
    "elastic": (".toml",),
    "kql": (".yaml", ".md", ".kql"),
    "auto": (".yml", ".yaml", ".toml", ".md", ".kql"),
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


def _tactics(names: List[str]) -> List[str]:
    return sorted({normalize_label(name) for name in names} & TACTICS)


def _sigma(doc: Dict[str, Any], rel: str) -> RuleRecord:
    tags = doc.get("tags") or []
    return RuleRecord(
        native_key=str(doc.get("id") or rel),
        name=str(doc.get("title") or doc.get("name") or rel),
        language="sigma",
        path=rel,
        query=_dump(doc.get("detection")),
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
        logic={"level": doc.get("level"), "logsource": doc.get("logsource")},
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


def _kql_yaml(doc: Dict[str, Any], rel: str) -> RuleRecord:
    return RuleRecord(
        native_key=str(doc.get("id") or rel),
        name=str(doc.get("title") or doc.get("name") or rel),
        language="kql",
        path=rel,
        query=str(doc.get("query") or ""),
        techniques=_techniques(doc.get("mitre") or []),
        content=_json_safe(doc),
    )


def _kql_markdown(text: str, rel: str) -> Optional[RuleRecord]:
    """A Hunting-Queries-Detection-Rules page: a title, a technique table, KQL blocks."""
    if not text.strip():
        return None
    path = Path(rel)
    title_match = re.search(r"^#\s+(.+)", text, re.MULTILINE)
    title = title_match.group(1).strip() if title_match else path.stem
    desc_match = re.search(
        r"####\s+Description\s*\n(.*?)(?=\n####|\n##|\n```|\Z)", text, re.DOTALL
    )
    blocks = re.findall(r"```(?:KQL|kql)\s*\n(.*?)```", text, re.DOTALL)
    query = "\n---\n".join(block.strip() for block in blocks)
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


def parse_file(fmt: str, path: Path, rel: str, text: str) -> Optional[RuleRecord]:
    """The rule a file holds under ``fmt``, or None when it holds none."""
    suffix = path.suffix
    if suffix == ".toml":
        return _elastic(text, rel)
    if suffix == ".md":
        if path.name.lower() in KQL_SKIPPED_DOCS:
            return None
        return _kql_markdown(text, rel)
    if suffix == ".kql":
        return _kql_file(text, rel)
    doc = yaml.load(text, Loader=_YAML_LOADER)  # nosec B506
    if not isinstance(doc, dict):
        return None
    if fmt == "auto":
        if "detection" in doc:
            fmt = "sigma"
        elif "search" in doc:
            fmt = "splunk"
        elif suffix == ".yaml":
            fmt = "kql"
        else:
            return None
    parsers: Dict[str, Callable[[Dict[str, Any], str], RuleRecord]] = {
        "sigma": _sigma,
        "splunk": _splunk,
        "kql": _kql_yaml,
    }
    return parsers[fmt](doc, rel)


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


def source_name(source: Dict[str, Any]) -> str:
    return f"repo:{source['id']}"


def read_source(source: Dict[str, Any], root: Path) -> List[RuleRecord]:
    """Every rule under ``root`` (the source's rules directory), keys unique."""
    if not root.is_dir():
        raise SourceUnreadable(f"{root} is not a directory")
    fmt = source.get("format") or "auto"
    suffixes = FORMAT_SUFFIXES.get(fmt, FORMAT_SUFFIXES["auto"])
    records: List[RuleRecord] = []
    keys: set = set()
    for path in _files(root, suffixes):
        rel = path.relative_to(root).as_posix()
        # An OSError is not a bad file: it fails the whole source.
        raw = path.read_bytes()
        try:
            # Postgres text cannot hold NUL, so such a file is not stored.
            record = (
                None
                if b"\x00" in raw
                else parse_file(fmt, path, rel, raw.decode("utf-8"))
            )
        except Exception:  # noqa: BLE001 -- a malformed file is not a rule
            record = None
        if record is None:
            continue
        # Two files with one id stay two rows: the first by path keeps the id.
        if record.native_key in keys:
            logger.warning(
                "detection rules: %s repeats id %s; keyed by path",
                rel,
                record.native_key,
            )
            record.native_key = f"{record.native_key}@{rel}"
        keys.add(record.native_key)
        records.append(record)
    return records
