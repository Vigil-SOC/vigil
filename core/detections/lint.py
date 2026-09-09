"""Lint Sigma rules for match keys tied to one environment.

A detection keyed to a literal address, hostname, account, or CIDR matches
one environment and generalises to nothing. Address/IOC matching belongs to
threat intel; this check stays on the rule's ``detection`` and ``logsource``
values so title/description/falsepositives cannot false-hit.
"""

from __future__ import annotations

import ipaddress
import re
from pathlib import Path
from typing import Any, Iterable, Optional

import yaml

_REWRITE = (
    "Rewrite around behaviour: process lineage, command-line arguments, "
    "parent-child relationships, or protocol/technique patterns — not a "
    "specific host, address, account, or subnet."
)

# IPv4 (optional CIDR). Candidates are confirmed with ipaddress.
_IPV4 = re.compile(r"(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?:/\d{1,2})?(?![\d.])")

# FQDNs whose last label is an environment TLD. Negative lookahead stops
# ``foo.local.exe`` matching as a ``.local`` host.
_ENV_HOST = re.compile(
    r"(?i)(?<![A-Za-z0-9.-])(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+"
    r"(?:corp|local|internal|lan)(?![A-Za-z0-9.-])"
)

_IPV4_BROADCAST = ipaddress.IPv4Address("255.255.255.255")

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
    seen: set[tuple[str, str, str]] = set()
    for section in ("detection", "logsource"):
        if section not in parsed:
            continue
        for field, value in _iter_strings(parsed[section]):
            for finding in _findings_for(field, value):
                key = (finding["kind"], finding["value"], finding["field"])
                if key in seen:
                    continue
                seen.add(key)
                findings.append(finding)

    passed = not findings
    return {
        "passed": passed,
        "title": parsed.get("title") if isinstance(parsed.get("title"), str) else None,
        "findings": findings,
        "rewrite_guidance": _guidance(findings) if findings else None,
    }


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

    return findings


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
            ipaddress.ip_network(token, strict=False)
        except ValueError:
            return None, None
        return "cidr", token
    try:
        ip = ipaddress.ip_address(token)
    except ValueError:
        return None, None
    if ip.is_loopback:
        return None, None
    if ip == _IPV4_BROADCAST:
        return None, None
    return "ip", token


def _is_user_field(field: str) -> bool:
    stem = _field_stem(field)
    if stem in _USER_FIELDS:
        return True
    return stem.endswith("username") or stem.endswith("accountname")


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
