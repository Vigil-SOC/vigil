"""Lint Sigma rules and native queries for match keys tied to one environment.

A detection keyed to a literal address, hostname, account, or CIDR matches
one environment and generalises to nothing. Address/IOC matching belongs to
threat intel; this check stays on the rule's ``detection`` and ``logsource``
values so title/description/falsepositives cannot false-hit. A native query
(``lint_query``: SPL, EQL, ES|QL, Lucene) is scanned whole for addresses and
hosts, and its ``field <op> value`` pairs for accounts and bare hostnames.

A whole IANA special-purpose block (10.0.0.0/8, 192.168.0.0/16, ...) is not
one environment: rules name it to mean "internal" or "external".
"""

from __future__ import annotations

import fnmatch
import ipaddress
import re
from pathlib import Path
from typing import Any, Iterable, Optional

import yaml

from core.detections.inventory.record import normalize_query

_REWRITE = (
    "Rewrite around behaviour: process lineage, command-line arguments, "
    "parent-child relationships, or protocol/technique patterns — not a "
    "specific host, address, account, or subnet. To leave out one host, "
    "address or account that is expected here, keep it out of the rule "
    "(no `not filter_*` naming it) and add an exception instead: "
    "save_detection_rule's exceptions, or detection_rule_exception on a "
    "saved copy."
)
_EXCEPTION_HINT = (
    "The rule leaves out a host by name. It still runs, but move that host to an "
    "exception (save_detection_rule's exceptions, or detection_rule_exception on "
    "a saved rule) so the rule itself names no machine."
)

# IPv4 (optional CIDR). Candidates are confirmed with ipaddress.
# Not after "<letter>/": a version (Chrome/120.0.0.0), not an address.
_IPV4 = re.compile(
    r"(?<![\d.])(?<![A-Za-z]/)(?:\d{1,3}\.){3}\d{1,3}(?:/\d{1,2})?(?![\d.])"
)

# FQDNs whose last label is an environment TLD. Negative lookahead stops
# ``foo.local.exe`` matching as a ``.local`` host.
_ENV_HOST = re.compile(
    r"(?i)(?<![A-Za-z0-9.-])(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+"
    r"(?:corp|local|internal|lan)(?![A-Za-z0-9.-])"
)

_IPV4_BROADCAST = ipaddress.IPv4Address("255.255.255.255")
# The IANA special-purpose registries' blocks, plus multicast: whole blocks only,
# a host or a narrower subnet in one stays a finding (a /32 or /128 entry is exempt).
_SPECIAL_BLOCKS = frozenset(
    ipaddress.ip_network(n)
    for n in (
        "0.0.0.0/8",
        "0.0.0.0/32",
        "10.0.0.0/8",
        "100.64.0.0/10",
        "127.0.0.0/8",
        "169.254.0.0/16",
        "172.16.0.0/12",
        "192.0.0.0/24",
        "192.0.0.0/29",
        "192.0.0.8/32",
        "192.0.0.9/32",
        "192.0.0.10/32",
        "192.0.0.170/32",
        "192.0.0.171/32",
        "192.0.2.0/24",
        "192.31.196.0/24",
        "192.52.193.0/24",
        "192.88.99.0/24",
        "192.168.0.0/16",
        "192.175.48.0/24",
        "198.18.0.0/15",
        "198.51.100.0/24",
        "203.0.113.0/24",
        "224.0.0.0/4",
        "240.0.0.0/4",
        "255.255.255.255/32",
        "::/128",
        "::1/128",
        "::ffff:0:0/96",
        "64:ff9b::/96",
        "64:ff9b:1::/48",
        "100::/64",
        "2001::/23",
        "2001::/32",
        "2001:1::1/128",
        "2001:1::2/128",
        "2001:2::/48",
        "2001:3::/32",
        "2001:4:112::/48",
        "2001:10::/28",
        "2001:20::/28",
        "2001:db8::/32",
        "2002::/16",
        "2620:4f:8000::/48",
        "fc00::/7",
        "fe80::/10",
        "ff00::/8",
    )
)

# Built-in / role accounts — not a specific person. Spec names SYSTEM,
# Administrator, root, krbtgt and wildcards; nearby role names are the same cut.
_WELL_KNOWN_USERS = frozenset(
    {
        "system",
        "administrator",
        "administrators",
        "admin",
        "root",
        "krbtgt",
        "guest",
        "defaultaccount",
        "localservice",
        "networkservice",
        "anonymouslogon",
        "anonymous",
        "everyone",
        "authenticatedusers",
        "wdagutilityaccount",
        "user",
        "users",
        # Service and daemon accounts, and placeholders.
        "nobody",
        "daemon",
        "bin",
        "sys",
        "www-data",
        "www",
        "apache",
        "apache2",
        "httpd",
        "nginx",
        "tomcat",
        "jboss",
        "wildfly",
        "postgres",
        "mysql",
        "oracle",
        "sshd",
        "mail",
        "ftp",
        "unknown",
        "null",
        "none",
    }
)

_USER_FIELDS = frozenset(
    {
        "user",
        "username",
        "userid",
        "account",
        "accountname",
        "targetuser",
        "targetusername",
        "subjectuser",
        "subjectusername",
        "srcuser",
        "destuser",
        "dstuser",
        "logonuser",
    }
)
# Fields that name the machine an event is on. Not DestinationHostname,
# *.domain, dns.question.name or url.*: those hold internet names (IOCs). Not
# Workstation / WorkstationName either: a logon event records there the name the
# remote client sends, so a rule names an attack tool's default (Rdesktop, RULER).
_HOST_FIELDS = frozenset(
    {
        "host",
        "hostname",
        "dest",
        "src",
        "dvc",
        "desthost",
        "srchost",
        "destnthost",
        "srcnthost",
        "computername",
        "computer",
    }
)
_HOST_PLACEHOLDERS = frozenset({"localhost", "unknown", "-", "null", "none", "n/a"})
_HOST_VALUE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
# Sigma modifiers whose value is a fragment or pattern, not one host.
_PARTIAL_MODIFIERS = frozenset({"contains", "startswith", "endswith", "re"})
_ENV_TLDS = frozenset({"corp", "local", "internal", "lan"})
_TOKEN = re.compile(r"[^\s,;'\"=]+")

_SID = re.compile(r"^S-\d+(-\d+)+$", re.IGNORECASE)
_PERSON_USER = re.compile(r"^[A-Za-z][A-Za-z0-9._-]{1,64}$")


def lint_sigma(
    *,
    rule_yaml: Optional[str] = None,
    source_path: Optional[str] = None,
) -> dict[str, Any]:
    """Lint a Sigma YAML string, or every ``.yml`` under a source path."""
    if (rule_yaml is None) == (source_path is None):
        return {
            "error": "Pass exactly one of rule_yaml or source_path",
            "passed": False,
        }
    if source_path is not None:
        return _lint_source(Path(source_path))
    return _lint_yaml(rule_yaml or "")


def _lint_source(root: Path) -> dict[str, Any]:
    if not root.exists():
        return {"error": f"Source path does not exist: {root}", "passed": False}

    files = [root] if root.is_file() else sorted(root.rglob("*.yml"))
    if root.is_file() and root.suffix.lower() != ".yml":
        return {"error": "Source file must be a .yml Sigma rule", "passed": False}

    results: list[dict[str, Any]] = []
    scanned = 0
    for path in files:
        if path.is_dir() or path.suffix.lower() != ".yml":
            continue
        scanned += 1
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            results.append(
                {
                    "file": str(path),
                    "passed": False,
                    "error": str(exc),
                }
            )
            continue
        report = _lint_yaml(text)
        report["file"] = str(path)
        if not report.get("passed"):
            results.append(report)

    rejected = len(results)
    return {
        "passed": rejected == 0,
        "rules_scanned": scanned,
        "rules_rejected": rejected,
        "results": results,
        "rewrite_guidance": _REWRITE if rejected else None,
    }


def _lint_yaml(text: str) -> dict[str, Any]:
    try:
        parsed = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        return {"passed": False, "error": f"YAML parse error: {exc}"}

    if parsed is None:
        return {"passed": True, "findings": [], "rewrite_guidance": None}
    if not isinstance(parsed, dict):
        return {"passed": False, "error": "Sigma rule YAML must be a mapping"}

    findings: list[dict[str, str]] = []
    warnings: list[dict[str, str]] = []
    seen: set[tuple[str, str, str]] = set()
    detection = parsed.get("detection")
    negated = _negated_selections(detection) if isinstance(detection, dict) else set()
    for section in ("detection", "logsource"):
        if section not in parsed:
            continue
        for field, value, selection in _section_strings(section, parsed[section]):
            for finding in _findings_for(field, value):
                if selection in negated and finding == _host_finding(field, value):
                    warnings.append(finding)
                    continue
                key = (finding["kind"], finding["value"], finding["field"])
                if key in seen:
                    continue
                seen.add(key)
                findings.append(finding)

    title = parsed.get("title")
    return _report(findings, warnings, title=title if isinstance(title, str) else None)


def _section_strings(
    section: str, node: Any
) -> Iterable[tuple[Optional[str], str, Optional[str]]]:
    """(field, value, selection) for each string; the selection is the detection
    key the value sits under."""
    if section == "detection" and isinstance(node, dict):
        for name, child in node.items():
            for field, value in _iter_strings(child, str(name)):
                yield field, value, str(name)
        return
    for field, value in _iter_strings(node):
        yield field, value, None


_NOT_SELECTION = re.compile(r"(?i)\bnot\s+(?:(?:1|all)\s+of\s+)?([\w*]+)")


def _negated_selections(detection: dict[str, Any]) -> set[str]:
    """Detection keys the condition excludes (``not filter``, ``not 1 of filter_*``)."""
    condition = detection.get("condition")
    conditions = condition if isinstance(condition, list) else [condition]
    patterns = [
        p for c in conditions if isinstance(c, str) for p in _NOT_SELECTION.findall(c)
    ]
    return {
        str(name)
        for name in detection
        if any(fnmatch.fnmatchcase(str(name), p) for p in patterns)
    }


_FIELD = r"(?<![\w.@])([A-Za-z_@][\w.@-]*)"
_QUOTED = r'"((?:[^"\\]|\\.)*)"|\'((?:[^\'\\]|\\.)*)\''
# field <op> value; a bare value is a literal only where the language says so.
_PAIR = re.compile(
    _FIELD
    + r"\s*(?:==|!=|=|:|(?i:like|in)~(?=\s|\()|(?i:like)(?=\s))\s*(?:"
    + _QUOTED
    + r"|([^\s()|,\"'=]+)(?![^\s()|,\"'=]*\s*\())"
)
# field IN (a, b) / field:(a OR b)
_LIST = re.compile(_FIELD + r"\s*(?:(?i:\s+in~?\s*)|:)\s*\(([^)]*)\)")
# A pipe outside double quotes: where an SPL command starts.
_SPL_PIPE = re.compile(r'\|(?=(?:[^"]*"[^"]*")*[^"]*$)')
_LIST_ITEM = re.compile(_QUOTED + r"|([^\s,]+)")
_SPL_SEARCHES = ("search", "tstats")  # search syntax: a bare value is a literal


def lint_query(query: str, language: str) -> dict[str, Any]:
    """Lint a native query: ``language`` is splunk, eql, esql or lucene."""
    text = normalize_query(query, language)
    findings: list[dict[str, str]] = []
    warnings: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()

    def add(items: list[dict[str, str]]) -> None:
        for item in items:
            if (item["kind"], item["value"]) not in seen:
                seen.add((item["kind"], item["value"]))
                findings.append(item)

    # Addresses and hosts anywhere in the query, whatever field holds them.
    add(_findings_for("query", text))
    for field, value, negated in _query_pairs(text, language):
        add([f for f in _findings_for(field, value) if f["kind"] == "user"])
        host = _host_finding(field, value)
        if host is None:
            continue
        if negated:
            warnings.append(host)
        else:
            add([host])
    return _report(findings, warnings, language=language)


def _report(
    findings: list[dict[str, str]], warnings: list[dict[str, str]], **extra: Any
) -> dict[str, Any]:
    """A lint result: findings fail it; warnings (one host left out by name) don't.
    A host that is also a finding is not repeated as a warning."""
    failing = {(f["kind"], f["value"].lower()) for f in findings}
    kept: dict[tuple[str, str], dict[str, str]] = {}
    for w in warnings:
        if (w["kind"], w["value"].lower()) not in failing:
            kept.setdefault((w["value"].lower(), w["field"]), w)
    return {
        "passed": not findings,
        **extra,
        "findings": findings,
        "rewrite_guidance": _guidance(findings) if findings else None,
        "warnings": list(kept.values()),
        "warning_guidance": _EXCEPTION_HINT if kept else None,
    }


_NATIVE = ("splunk", "eql", "esql", "lucene")


def lint_record(record: Any) -> Optional[dict[str, Any]]:
    """``lint_query`` on a staged overlay copy's RuleRecord, or None for a
    language Vigil does not run (an Elastic kuery copy)."""
    if record.language == "splunk":
        return lint_query(record.query, "splunk")
    language = record.logic.get("language")
    return lint_query(record.query, language) if language in _NATIVE else None


def _query_pairs(text: str, language: str) -> Iterable[tuple[str, str, bool]]:
    """(field, literal, negated) triples. EQL/ES|QL bare words, and SPL where's,
    are fields. ``negated``: the pair is excluded (!=, NOT IN, or NOT/not/!/- just
    before it or before its opening parenthesis)."""
    if language == "splunk":
        segments = _SPL_PIPE.split(text)
        parts = [(segments[0], True)]
        for seg in segments[1:]:
            command = seg.strip().split(" ", 1)[0].lower()
            if command in _SPL_SEARCHES or command == "where":
                parts.append((seg, command != "where"))
    else:
        parts = [(text, language == "lucene")]
    for part, bare in parts:
        for m in _PAIR.finditer(part):
            value = m.group(2) if m.group(2) is not None else m.group(3)
            if value is None and bare:
                value = m.group(4)
            if value is not None:
                op = m.group(0)[len(m.group(1)) :].lstrip()
                negated = op.startswith("!=") or _negated_before(part, m.start())
                yield m.group(1), value.replace("\\\\", "\\"), negated
        for m in _LIST.finditer(part):
            op = m.group(0)[len(m.group(1)) : m.group(0).index("(")]
            negated = bool(re.search(r"(?i)\bnot\b", op)) or _negated_before(
                part, m.start()
            )
            for item in _LIST_ITEM.finditer(m.group(2)):
                value = item.group(1) if item.group(1) is not None else item.group(2)
                if (
                    value is None
                    and bare
                    and item.group(3).upper() not in ("OR", "AND", "NOT")
                ):
                    value = item.group(3)
                if value is not None:
                    yield m.group(1), value, negated


_NOT_BEFORE = re.compile(r"(?i)(?:\bnot|!)\s*\(?\s*$|(?:^|[\s(])-$")


def _negated_before(part: str, start: int) -> bool:
    """Whether NOT, not, ! (or Lucene's -) stands just before ``start``."""
    return bool(_NOT_BEFORE.search(part[max(0, start - 12) : start]))


def _iter_strings(
    node: Any, field: Optional[str] = None
) -> Iterable[tuple[Optional[str], str]]:
    if isinstance(node, dict):
        for key, child in node.items():
            yield from _iter_strings(child, str(key) if key is not None else field)
        return
    if isinstance(node, list):
        for child in node:
            yield from _iter_strings(child, field)
        return
    if isinstance(node, str):
        yield field, node


def _findings_for(field: Optional[str], value: str) -> list[dict[str, str]]:
    findings: list[dict[str, str]] = []
    field_name = field or ""

    for token in _IPV4.findall(value):
        kind, literal = _classify_ip_token(token)
        if kind is None or literal is None:
            continue
        findings.append(_finding(kind, literal, field_name))

    for token in _TOKEN.findall(value):
        ip_token = _unbracket_ip(token)
        if ":" not in ip_token:
            continue
        kind, literal = _classify_ip_token(ip_token)
        if kind is None or literal is None:
            continue
        findings.append(_finding(kind, literal, field_name))

    hosts = _ENV_HOST.findall(value)
    for host in hosts:
        findings.append(_finding("hostname", host, field_name))
    if not hosts and _is_env_host_suffix(value):
        findings.append(_finding("hostname", value.strip(), field_name))

    if _is_user_field(field_name):
        person = _specific_person(value)
        if person is not None:
            findings.append(_finding("user", person, field_name))

    host = _host_finding(field_name, value)
    if host is not None and host["value"].lower() not in {h.lower() for h in hosts}:
        findings.append(host)

    return findings


def _host_finding(field: Optional[str], value: str) -> Optional[dict[str, str]]:
    """The finding for one bare host named on a host field, or None."""
    if not field or not _is_host_field(field):
        return None
    host = _single_host(value)
    return _finding("hostname", host, field) if host is not None else None


def _unbracket_ip(token: str) -> str:
    if token.startswith("[") and token.endswith("]") and token.count("]") == 1:
        return token[1:-1]
    return token


def _is_env_host_suffix(value: str) -> bool:
    """``*.local`` / ``.corp.local`` — environment TLD without a host label."""
    token = value.strip().strip("'\"")
    if token.startswith("*"):
        token = token[1:]
    if not token.startswith("."):
        return False
    labels = [part for part in token.lower().split(".") if part]
    return bool(labels) and labels[-1] in _ENV_TLDS


def _classify_ip_token(token: str) -> tuple[Optional[str], Optional[str]]:
    if "/" in token:
        try:
            network = ipaddress.ip_network(token, strict=False)
        except ValueError:
            return None, None
        if network in _SPECIAL_BLOCKS:
            return None, None
        return "cidr", token
    try:
        ip = ipaddress.ip_address(token)
    except ValueError:
        return None, None
    if ip.is_loopback:
        return None, None
    if ip == _IPV4_BROADCAST or ipaddress.ip_network(ip) in _SPECIAL_BLOCKS:
        return None, None
    return "ip", token


def _is_user_field(field: str) -> bool:
    # ECS keeps the account in user.name (source.user.name, user.target.name, ...),
    # whose stem is "name".
    parts = field.split("|", 1)[0].lower().split(".")
    if parts[-1] == "name" and "user" in parts[:-1]:
        return True
    stem = _field_stem(field)
    if stem in _USER_FIELDS:
        return True
    return stem.endswith("username") or stem.endswith("accountname")


def _is_host_field(field: str) -> bool:
    base, _, mods = field.partition("|")
    if _PARTIAL_MODIFIERS & set(mods.lower().split("|")):
        return False
    if base.lower().startswith("esql."):
        return False  # a column the query computes: often an internet domain
    parts = base.lower().split(".")
    # ECS host.name / host.hostname, and *.host.name under another object.
    if len(parts) > 1 and parts[-2] == "host" and parts[-1] in ("name", "hostname"):
        return True
    return _field_stem(base) in _HOST_FIELDS


def _single_host(value: str) -> Optional[str]:
    """A literal single host name: not a pattern, placeholder or IP address."""
    raw = value.strip().strip("'\"")
    if raw.lower() in _HOST_PLACEHOLDERS or not _HOST_VALUE.match(raw):
        return None
    if not any(c.isalpha() for c in raw):
        return None  # a number or IP: the address checks own those
    if "." in raw:
        # Not bare: the environment-host check owns .corp/.local/.internal/.lan
        # names, and any other dotted name is an internet domain (an IOC).
        return None
    try:
        ipaddress.ip_address(raw)
        return None
    except ValueError:
        return raw


def _field_stem(field: str) -> str:
    base = field.split("|", 1)[0]
    last = base.split(".")[-1]
    return last.lower().replace("-", "").replace("_", "")


def _specific_person(value: str) -> Optional[str]:
    raw = value.strip().strip("'\"")
    if not raw or "*" in raw or "?" in raw:
        return None
    principal = raw.split("\\")[-1]
    principal = principal.split("@")[0].strip()
    if not principal or _SID.match(principal):
        return None
    if principal.endswith("$"):
        return None
    folded = principal.lower().replace(" ", "")
    if folded in _WELL_KNOWN_USERS:
        return None
    if not _PERSON_USER.match(principal):
        return None
    return principal


def _finding(kind: str, value: str, field: str) -> dict[str, str]:
    return {"kind": kind, "value": value, "field": field}


def _guidance(findings: list[dict[str, str]]) -> str:
    literals = ", ".join(f"{item['kind']} {item['value']}" for item in findings)
    return f"Rule is keyed to environment-specific literals ({literals}). {_REWRITE}"
