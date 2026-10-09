"""Reference log fingerprinter for contracts/fingerprint.md (algorithm fp1).

This is the executable spec, not production code: D5/E4 port it into the watcher
and must produce identical fingerprints for contracts/fixtures/fingerprint/*.json.
Every regex runs on RE2 (linear time, K1 T-10).
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field

import re2

ALGO = "fp1"
MAX_LINE = 16 * 1024
MAX_NORMALIZED = 160
MAX_FRAMES = 5
FIRST_PARTY = ("core/", "services/")

# Closed, shape-independent service names (X1 #6). fp1 hashes the service, so a sensor
# reports Helm's "<fullname>-soc-daemon" as "soc-daemon". Mirrors observation $defs/service.
SERVICES = (
    "backend",
    "soc-daemon",
    "llm-worker",
    "agent-worker",
    "agent-serve",
    "bifrost",
    "backup",
    "postgres",
    "redis",
    "host",
    "medic",
    "medic-gateway",
)
_LABEL_SAFE = re2.compile(r"^[A-Za-z0-9_.:/@+-]{1,128}$")

# Order matters: earlier rules consume text that later rules would split.
_SUBSTITUTIONS: list[tuple[re2._Regexp, str]] = [
    (re2.compile(r"\x1b\[[0-9;]*m"), ""),
    (re2.compile(r"\b[a-zA-Z][a-zA-Z0-9+.-]*://[^\s'\"<>]+"), "<url>"),
    (re2.compile(r"\b[\w.+-]+@[\w-]+(\.[\w-]+)+\b"), "<email>"),
    (re2.compile(r"'[^']*'|\"[^\"]*\""), "<str>"),
    (
        re2.compile(
            r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"
        ),
        "<uuid>",
    ),
    (
        re2.compile(
            r"\b\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:?\d{2})?"
        ),
        "<ts>",
    ),
    (re2.compile(r"(/[\w.-]+){2,}/?"), "<path>"),
    (re2.compile(r"\b\d{1,3}(\.\d{1,3}){3}(:\d+)?\b"), "<ip>"),
    (re2.compile(r"\b(0x)?[0-9a-fA-F]{8,}\b"), "<hex>"),
    (re2.compile(r"\b\d+(\.\d+)?"), "<num>"),
    (re2.compile(r"\s+"), " "),
]

# Python levelnames and the agent services' lowercase levels (services/agent/core/log.ts).
_LEVELS = {
    "debug": "DEBUG",
    "info": "INFO",
    "warn": "WARNING",
    "warning": "WARNING",
    "error": "ERROR",
    "fatal": "CRITICAL",
    "critical": "CRITICAL",
}
_PLACEHOLDER = re2.compile(r"%(\([a-zA-Z_]+\))?[-#0 +]*\d*(\.\d+)?[sdifrxXeEgGc%]")
_FRAME = re2.compile(r'File "([^"]+)", line \d+, in ([A-Za-z0-9_<>.]+)')
_TEXT_DAEMON = re2.compile(
    r"^\S+ \S+ - ([A-Za-z0-9_.]+) - (DEBUG|INFO|WARNING|ERROR|CRITICAL) - (.*)$"
)
_TEXT_LEVEL = re2.compile(r"^(DEBUG|INFO|WARNING|ERROR|CRITICAL):? (.*)$")


def label_safe(value: str) -> str:
    """D3's label rule, applied to every label and group value at extraction (X1 #1).

    A value that doesn't fit the label pattern becomes 'h_' + 16 hex of its sha256, so
    injected text can't ride in a group key. The raw value travels only as a K2-redacted
    evidence excerpt.

    Values that look like the engine's own markers ('h_…', '__overflow__') are hashed
    too, so a raw value can't forge a hashed group or the overflow group (S0 review R10).
    Apply it exactly once, at extraction: it is deliberately not idempotent.
    """
    if _LABEL_SAFE.match(value) and not value.startswith(("h_", "__")):
        return value
    return "h_" + hashlib.sha256(value.encode()).hexdigest()[:16]


@dataclass(frozen=True)
class LogLine:
    service: str
    format: str  # python_json | agent_json | text
    level: str | None = None
    logger: str | None = None
    template: str | None = None  # msg_template (Python) or msg (agents)
    message: str | None = None
    exc_type: str | None = None
    exception: str | None = None  # Python traceback text
    raw: str | None = None  # text format only


@dataclass(frozen=True)
class Fingerprint:
    fingerprint: str
    basis: str  # template | normalized
    template_trust: str  # catalog | untrusted
    level: str
    logger: str
    frames: tuple[str, ...]
    args: tuple[str, ...] = field(default_factory=tuple)


def normalize(text: str) -> str:
    """Strip variable parts from untrusted text (fingerprint.md §3)."""
    out = text[:MAX_LINE].split("\n", 1)[0]
    for pattern, repl in _SUBSTITUTIONS:
        out = pattern.sub(repl, out)
    return out.strip()[:MAX_NORMALIZED]


def frames_of(exception: str | None) -> tuple[str, ...]:
    """Innermost first-party frames of the last traceback block, no line numbers."""
    if not exception:
        return ()
    last_block = exception.split("During handling of the above exception")[-1]
    last_block = last_block.split("The above exception was the direct cause")[-1]
    found = []
    for path, func in _FRAME.findall(last_block):
        for root in FIRST_PARTY:
            idx = path.rfind("/" + root)
            if path.startswith(root):
                found.append(f"{path}:{func}")
                break
            if idx != -1 and "site-packages" not in path:
                found.append(f"{path[idx + 1 :]}:{func}")
                break
    return tuple(found[-MAX_FRAMES:])


def template_trust(line: LogLine, catalog: set[tuple[str, str]]) -> str:
    """'catalog' only for an exact (logger, template) catalog hit that was really
    rendered from args (fingerprint.md §2). Everything else is untrusted."""
    if not line.template or (line.logger or "", line.template) not in catalog:
        return "untrusted"
    if _PLACEHOLDER.search(line.template) and line.message == line.template:
        return "untrusted"  # an f-string that happens to equal a catalog entry
    return "catalog"


def recover_args(template: str, message: str) -> tuple[str, ...]:
    """Align a %-style template against its rendered message. Args are untrusted."""
    literals, pos = [], 0
    for m in _PLACEHOLDER.finditer(template):
        literals.append(template[pos : m.start()])
        pos = m.end()
    literals.append(template[pos:])
    if len(literals) < 2:
        return ()
    pattern = "^" + "(.*?)".join(re2.escape(lit) for lit in literals) + "$"
    match = re2.compile(pattern).match(message)
    return tuple(a[:200] for a in match.groups())[:8] if match else ()


def parse_text(line: LogLine) -> LogLine:
    raw = (line.raw or "")[:MAX_LINE]
    m = _TEXT_DAEMON.match(raw)
    if m:
        return LogLine(line.service, "text", m.group(2), m.group(1), None, m.group(3))
    m = _TEXT_LEVEL.match(raw)
    if m:
        return LogLine(line.service, "text", m.group(1), None, None, m.group(2))
    return LogLine(line.service, "text", line.level or "INFO", None, None, raw)


def fingerprint(
    line: LogLine, catalog: set[tuple[str, str]] | None = None
) -> Fingerprint:
    if line.service not in SERVICES:
        raise ValueError(f"service {line.service!r} is not in SERVICES (X1 #6)")
    if line.format == "text":
        line = parse_text(line)
    catalog = catalog or set()
    trust = template_trust(line, catalog)
    if trust == "catalog":
        basis, basis_text = "template", " ".join(line.template.split())
    else:
        basis, basis_text = "normalized", normalize(line.template or line.message or "")
    frames = frames_of(line.exception)
    frames_sig = (
        hashlib.sha256("|".join(frames).encode()).hexdigest()[:16] if frames else ""
    )
    level = _LEVELS.get((line.level or "info").lower(), "INFO")
    logger = line.logger or ""
    # basis is hashed so an untrusted line can never share a trusted line's fingerprint.
    parts = [
        ALGO,
        basis,
        line.service,
        logger,
        level,
        basis_text,
        line.exc_type or "",
        frames_sig,
    ]
    digest = hashlib.sha256("\x1f".join(parts).encode()).hexdigest()[:16]
    args = ()
    if line.template and line.message and _PLACEHOLDER.search(line.template):
        args = recover_args(line.template, line.message)
    return Fingerprint(f"fp1_{digest}", basis, trust, level, logger, frames, args)
