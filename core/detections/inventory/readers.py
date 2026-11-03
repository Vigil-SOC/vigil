"""One reader per source. Each returns every rule the source holds, or raises.

A reader never returns a partial list: sync marks whatever a list leaves out
as removed, so a reader that cannot read everything must raise instead.
"""

from __future__ import annotations

import json
import logging
import subprocess
import tomllib
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Optional, Tuple

import httpx
import yaml

from core.detections.inventory.record import RuleRecord, techniques_in
from core.integrations._base.config import missing, resolve, resolve_fields
from core.integrations._base.tls import tls_verify

logger = logging.getLogger(__name__)

TIMEOUT = httpx.Timeout(connect=10.0, read=120.0, write=10.0, pool=5.0)
KIBANA_PAGE = 1000
WAZUH_PAGE = 500

# Splunk saved-search keys that change what fires, besides the search itself.
SPLUNK_LOGIC_KEYS = (
    "alert.severity",
    "alert_type",
    "alert_comparator",
    "alert_threshold",
    "alert_condition",
    "dispatch.earliest_time",
    "dispatch.latest_time",
)

# Kibana detection-rule fields (and the same keys under a repo toml's [rule])
# that change what fires or how loud it is, besides query and language.
ELASTIC_LOGIC_KEYS = (
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

# Kibana fields that change on every run or save, kept out of raw.
_KIBANA_VOLATILE = ("updated_at", "updated_by", "revision", "execution_summary")


class SourceNotConfigured(Exception):
    """The source has no connection settings; sync skips it."""


def _bool(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes")
    return bool(value)


# --- Splunk -----------------------------------------------------------------


def _splunk_service():
    from core.integrations.splunk.client import SplunkService
    from core.integrations.splunk.descriptor import SPLUNK

    config = resolve(SPLUNK)
    if missing(config, "server_url", "username", "password"):
        raise SourceNotConfigured("splunk")
    return SplunkService(
        server_url=config["server_url"],
        username=config["username"],
        password=config["password"],
        verify_ssl=config["verify_ssl"],
        ca_cert_path=config.get("ca_cert_path"),
    )


def splunk_record(entry: Dict[str, Any]) -> Optional[RuleRecord]:
    """A saved search as a rule, or None when it is neither scheduled nor a correlation search."""
    content = entry.get("content") or {}
    scheduled = _bool(content.get("is_scheduled"))
    correlation = _bool(content.get("action.correlationsearch.enabled"))
    if not (scheduled or correlation):
        return None
    app = (entry.get("acl") or {}).get("app") or content.get("eai:acl", {}).get("app")
    name = entry["name"]
    annotations = content.get("action.correlationsearch.annotations")
    return RuleRecord(
        native_key=f"{app}/{name}",
        name=name,
        query=content.get("search") or "",
        language="spl",
        enabled=not _bool(content.get("disabled")),
        techniques=techniques_in([annotations]),
        logic={k: content.get(k) for k in SPLUNK_LOGIC_KEYS},
        raw={k: v for k, v in content.items() if not k.startswith("eai:")},
        seen_in=f"splunk:{app}",
    )


def read_splunk() -> List[RuleRecord]:
    service = _splunk_service()
    if not service.authenticate():
        raise RuntimeError("splunk login failed")
    try:
        response = service._request(
            "GET",
            f"{service.server_url}/servicesNS/-/-/saved/searches",
            params={"output_mode": "json", "count": 0},
        )
        response.raise_for_status()
        entries = response.json().get("entry") or []
    finally:
        service.session.close()
    return [r for r in (splunk_record(e) for e in entries) if r is not None]


# --- Elastic (Kibana detection engine) ----------------------------------------


def elastic_record(
    rule: Dict[str, Any], native_key: str, seen_in: str, raw: Any
) -> RuleRecord:
    """A Kibana rule, or a repo toml's [rule] table, as a rule."""
    return RuleRecord(
        native_key=native_key,
        name=rule.get("name") or native_key,
        query=rule.get("query") or "",
        language=rule.get("language") or rule.get("type") or "",
        enabled=_bool(rule.get("enabled", True)),
        techniques=techniques_in([rule.get("threat")]),
        logic={k: rule.get(k) for k in ELASTIC_LOGIC_KEYS},
        raw=raw,
        seen_in=seen_in,
    )


def read_kibana() -> List[RuleRecord]:
    from core.integrations.elastic.descriptor import ELASTIC

    config = resolve(ELASTIC)
    if missing(config, "kibana_url"):
        raise SourceNotConfigured("elastic")
    headers = {}
    auth = None
    if config.get("api_key"):
        headers["Authorization"] = f"ApiKey {config['api_key']}"
    elif config.get("username"):
        auth = httpx.BasicAuth(config["username"], config.get("password") or "")
    verify = True if config.get("verify_ssl") is None else config["verify_ssl"]
    records: List[RuleRecord] = []
    with httpx.Client(
        base_url=config["kibana_url"].rstrip("/"),
        auth=auth,
        headers=headers,
        verify=tls_verify(verify, config.get("ca_cert_path")),
        timeout=TIMEOUT,
    ) as client:
        page = 1
        while True:
            response = client.get(
                "/api/detection_engine/rules/_find",
                params={
                    "page": page,
                    "per_page": KIBANA_PAGE,
                    "sort_field": "created_at",
                    "sort_order": "asc",
                },
            )
            response.raise_for_status()
            body = response.json()
            for rule in body.get("data") or []:
                raw = {k: v for k, v in rule.items() if k not in _KIBANA_VOLATILE}
                records.append(
                    elastic_record(
                        rule, rule["rule_id"], "kibana:detection_engine", raw
                    )
                )
            if page * KIBANA_PAGE >= int(body.get("total") or 0):
                break
            page += 1
        total = int(body.get("total") or 0)
    # Pages are not a snapshot: a rule saved mid-read can shift one onto the
    # next page twice or off it. Short or doubled, the list is not complete.
    if len({r.native_key for r in records}) != total or len(records) != total:
        raise RuntimeError(f"kibana listed {len(records)} rules, total says {total}")
    return records


# --- Wazuh server API -------------------------------------------------------

WAZUH_FIELDS = ("api_url", "username", "password", "verify_ssl")


def wazuh_record(rule: Dict[str, Any], raw_xml: Optional[str]) -> RuleRecord:
    details = rule.get("details") or {}
    return RuleRecord(
        native_key=str(rule["id"]),
        name=rule.get("description") or f"wazuh rule {rule['id']}",
        query=json.dumps(details, sort_keys=True),
        language="wazuh-xml",
        enabled=(rule.get("status") or "enabled") == "enabled",
        techniques=techniques_in([rule.get("mitre")]),
        logic={"level": rule.get("level")},
        raw={"rule": rule, "xml": raw_xml},
        seen_in=f"wazuh:{rule.get('relative_dirname')}/{rule.get('filename')}",
    )


def _wazuh_rule_xml(file_xml: str, rule_id: str) -> Optional[str]:
    import re

    match = re.search(
        rf'<rule\s[^>]*\bid="{re.escape(rule_id)}"[^>]*>.*?</rule>', file_xml, re.DOTALL
    )
    return match.group(0) if match else None


def read_wazuh() -> List[RuleRecord]:
    config = resolve_fields("wazuh", WAZUH_FIELDS)
    if missing(config, "api_url", "username", "password"):
        raise SourceNotConfigured("wazuh")
    verify_ssl = config.get("verify_ssl")
    verify = True if verify_ssl in (None, "") else _bool(verify_ssl)
    with httpx.Client(
        base_url=str(config["api_url"]).rstrip("/"),
        verify=tls_verify(verify, None),
        timeout=TIMEOUT,
    ) as client:
        login = client.post(
            "/security/user/authenticate",
            params={"raw": "true"},
            auth=(config["username"], config["password"]),
        )
        login.raise_for_status()
        client.headers["Authorization"] = f"Bearer {login.text.strip()}"

        rules: List[Dict[str, Any]] = []
        offset = 0
        while True:
            response = client.get(
                "/rules", params={"limit": WAZUH_PAGE, "offset": offset}
            )
            response.raise_for_status()
            data = response.json()["data"]
            rules.extend(data["affected_items"])
            offset += WAZUH_PAGE
            if offset >= int(data["total_affected_items"]):
                break
        if len(rules) != int(data["total_affected_items"]):
            raise RuntimeError(
                f"wazuh listed {len(rules)} of {data['total_affected_items']} rules"
            )

        files: Dict[Tuple[str, str], Optional[str]] = {}
        for rule in rules:
            key = (rule.get("relative_dirname") or "", rule.get("filename") or "")
            if key in files:
                continue
            response = client.get(
                f"/rules/files/{key[1]}",
                params={"raw": "true", "relative_dirname": key[0]},
            )
            files[key] = response.text if response.status_code == 200 else None

    return [
        wazuh_record(
            rule,
            _wazuh_rule_xml(
                files.get(
                    (rule.get("relative_dirname") or "", rule.get("filename") or "")
                )
                or "",
                str(rule["id"]),
            ),
        )
        for rule in rules
    ]


# --- Git repos and local directories ----------------------------------------


def _git_head(path: Path) -> str:
    try:
        out = subprocess.run(
            ["git", "-C", str(path), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=10,
            check=True,
        )
        return out.stdout.strip()[:12]
    except Exception:  # noqa: BLE001 -- a local directory has no commit
        return "local"


def _sigma(path: Path, rel: str, text: str) -> Optional[RuleRecord]:
    doc = yaml.safe_load(text)
    if not isinstance(doc, dict) or "detection" not in doc:
        return None
    return RuleRecord(
        native_key=str(doc.get("id") or rel),
        name=str(doc.get("title") or rel),
        query=json.dumps(doc.get("detection"), sort_keys=True, default=str),
        language="sigma",
        enabled=str(doc.get("status") or "") != "deprecated",
        techniques=techniques_in([doc.get("tags")]),
        logic={"level": doc.get("level"), "logsource": doc.get("logsource")},
        raw=text,
    )


def _escu(path: Path, rel: str, text: str) -> Optional[RuleRecord]:
    doc = yaml.safe_load(text)
    if not isinstance(doc, dict) or "search" not in doc:
        return None
    tags = doc.get("tags") or {}
    return RuleRecord(
        native_key=str(doc.get("id") or rel),
        name=str(doc.get("name") or rel),
        query=str(doc.get("search") or ""),
        language="spl",
        enabled=str(doc.get("status") or "") != "deprecated",
        techniques=techniques_in([tags.get("mitre_attack_id")]),
        logic={"type": doc.get("type")},
        raw=text,
    )


def _elastic_toml(path: Path, rel: str, text: str) -> Optional[RuleRecord]:
    doc = tomllib.loads(text)
    rule = doc.get("rule")
    if not isinstance(rule, dict):
        return None
    meta = doc.get("metadata") or {}
    record = elastic_record(rule, str(rule.get("rule_id") or rel), "", text)
    record.enabled = meta.get("maturity") != "deprecated"
    return record


def _kql(path: Path, rel: str, text: str) -> Optional[RuleRecord]:
    import re

    blocks = re.findall(r"```(?:kql|KQL|kusto)?\s*\n(.*?)```", text, re.DOTALL)
    if path.suffix == ".md" and not blocks:
        return None
    query = "\n".join(blocks) if blocks else text
    return RuleRecord(
        native_key=rel,
        name=path.stem,
        query=query,
        language="kql-ms",
        techniques=techniques_in([text]),
        raw=text,
    )


def _auto(path: Path, rel: str, text: str) -> Optional[RuleRecord]:
    if path.suffix == ".toml":
        return _elastic_toml(path, rel, text)
    if path.suffix in (".yml", ".yaml"):
        doc = yaml.safe_load(text)
        if isinstance(doc, dict) and "detection" in doc:
            return _sigma(path, rel, text)
        if isinstance(doc, dict) and "search" in doc:
            return _escu(path, rel, text)
        return None
    return _kql(path, rel, text)


PARSERS: Dict[str, Callable[[Path, str, str], Optional[RuleRecord]]] = {
    "sigma": _sigma,
    "splunk": _escu,
    "elastic": _elastic_toml,
    "kql": _kql,
    "auto": _auto,
}


def repo_files(root: Path, extensions: List[str]) -> Iterator[Path]:
    for path in sorted(root.rglob("*")):
        if path.is_file() and path.suffix in extensions and ".git" not in path.parts:
            yield path


def read_repo(source: Dict[str, Any]) -> List[RuleRecord]:
    """Every rule file of one configured repo or directory. Unparseable files are skipped."""
    from core.detections.detection_rules_service import FORMAT_EXTENSIONS

    base = Path(source["local_path"])
    root = base / (source.get("subdirectory") or "")
    if not root.is_dir():
        raise RuntimeError(f"{root} is not a directory")
    fmt = source.get("format") or "auto"
    parse = PARSERS[fmt]
    commit = _git_head(base)
    records: List[RuleRecord] = []
    skipped = 0
    for path in repo_files(root, FORMAT_EXTENSIONS.get(fmt, FORMAT_EXTENSIONS["auto"])):
        rel = str(path.relative_to(base))
        try:
            record = parse(path, rel, path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001 -- a malformed file is not a rule
            record = None
        if record is None:
            skipped += 1
            continue
        record.seen_in = f"{source_name(source)}@{commit}:{rel}"
        records.append(record)
    if skipped:
        logger.info(
            "rule inventory: %s skipped %d non-rule files", source_name(source), skipped
        )
    return records


def source_name(source: Dict[str, Any]) -> str:
    return f"repo:{source.get('clone_name') or Path(source['local_path']).name}"


def repo_sources() -> List[Dict[str, Any]]:
    """The detection sources Settings lists that are on disk."""
    from core.detections.detection_rules_service import DetectionRulesService

    return [
        s
        for s in DetectionRulesService().sources
        if s.get("enabled", True) and s.get("local_path") and s.get("status") == "ready"
    ]
