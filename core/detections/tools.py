"""
Security-Detections tool integration - Pure Python implementation
Answers from the detection rules every configured repo holds (Sigma, Splunk, Elastic, KQL)
and the rules Kibana's detection engine and the Wazuh manager run, as
core/detections/inventory syncs them into Postgres. Splunk saved searches are not read yet.
"""

import fnmatch
import re
from typing import Any, Callable, Dict, List, NoReturn, Optional

import yaml

from core.agents.projections import read_projection
from core.detections.gaps import (
    FINDINGS_LIMIT,
    gap_report,
    missed_runs_report,
    read_alert_labels,
    read_ledger,
)
from core.detections.inventory import catalog
from core.detections.inventory.links import link_rule
from core.detections.lint import lint_sigma
from core.detections.reconstruction import (
    coverage_report,
    reconstruct,
    span_for_steps,
    steps_from_dispatch_results,
)
from core.storage.database_data_service import DatabaseDataService


class SecurityDetectionsTools:
    """
    Main class for security detection tools integration.
    Reads the detection_rules tables; the inventory sync keeps them current.
    """

    async def analyze_coverage(
        self,
        techniques: Optional[List[str]] = None,
        run_id: Optional[str] = None,
        steps: Any = None,
        **_kwargs: object,
    ) -> Dict:
        """Catalog counts for techniques, or a run report when run_id / steps is set.

        Extra kwargs (including ``limit`` injected by ``/internal/tools/invoke``)
        are ignored. A run id or action trace is the report path, not catalog
        counts mixed into the same rows.
        """
        trace_given = isinstance(steps, list)
        if (trace_given and steps) or run_id or (trace_given and not techniques):
            return await self._run_coverage(
                run_id=run_id,
                steps=steps if trace_given else None,
            )
        if techniques is None:
            return {
                "error": "techniques is required unless run_id or steps is provided"
            }

        return {
            technique: catalog.rules_for_technique(technique.upper())
            for technique in techniques
        }

    async def _run_coverage(self, run_id: Optional[str], steps: Any) -> Dict:
        if isinstance(steps, list) and steps:
            trace: List = steps
        elif run_id:
            projection = await read_projection(run_id)
            if projection is None:
                return {
                    "run_id": run_id,
                    "techniques": [],
                    "error": "no readable run",
                }
            trace = steps_from_dispatch_results(projection.get("results"))
        else:
            trace = steps if isinstance(steps, list) else []
        reconstructed = await self.reconstruct_run(steps=trace)
        report = coverage_report(trace, reconstructed)
        if run_id:
            report["run_id"] = run_id
        return report

    async def search_detections(
        self,
        query: str = "",
        source_type: Optional[str] = None,
        limit: int = 20,
        tool: Optional[str] = None,
        runs: Optional[str] = None,
        sees: Optional[str] = None,
    ) -> List[Dict]:
        """
        Search across all detection rules using keywords.

        Args:
            query: Search query (e.g., "powershell base64", "lateral movement")
            source_type: Optional filter by rule format ("sigma", "splunk", "elastic", "kql", "wazuh")
            limit: Maximum number of results
            tool: Optional filter by where the rule was read ("repo", "elastic", "wazuh")
            runs: Optional filter by Runs result ("pass", "fail", "unknown")
            sees: Optional filter by Sees result ("pass", "fail", "unknown")

        Returns:
            List of matching detection rules, each with its status and checks
        """
        return catalog.search(query or "", source_type, limit, tool, runs, sees)

    async def link_detection_rule(
        self,
        copy_rule_id: Any = None,
        action: str = "",
        reason: str = "",
        origin_rule_id: Any = None,
        **_kwargs: object,
    ) -> Dict:
        """Link, unlink or decline a SIEM copy and the repo rule it came from.

        Args:
            copy_rule_id: The SIEM rule's rule_id (a search_detections hit with tool != 'repo')
            action: 'link', 'unlink' or 'decline'
            reason: Why; required, and written to the audit log
            origin_rule_id: The repo rule's rule_id; required to link or decline
        """
        return link_rule(copy_rule_id, action, reason, origin_rule_id)

    async def get_detection_count(self, source_type: Optional[str] = None) -> Dict:
        """
        Get count of detections by source.

        Args:
            source_type: Optional filter by source

        Returns:
            Count information
        """
        counts = catalog.rule_counts()
        if source_type:
            return {"source": source_type, "count": counts.get(source_type, 0)}
        return {"total": sum(counts.values()), "by_source": counts}

    async def identify_gaps(
        self, context: str = "", since_days: int = 30, **_kwargs: object
    ) -> Dict:
        """Techniques seen on stored alerts that no active rule declares, with what it cannot say,
        and the proven hunts and closed Cases no rule alerted on (``runs_rules_missed``).

        Args:
            context: A case id, an ATT&CK label to match, or empty for every stored alert.
            since_days: How far back Verdicts are read for runs_rules_missed.
        """
        # A model may send "30"; keep the window within what timedelta can hold.
        since_days = min(max(int(since_days), 0), 3650)
        alerts = read_alert_labels(context)
        rules_by_technique = catalog.rules_by_technique()
        report = gap_report(
            context,
            alerts,
            rules_by_technique,
            catalog.rules_by_tactic(),
            catalog.rule_counts(),
            catalog.siem_checks_by_technique(),
            catalog.blind_spots(),
        )
        data = DatabaseDataService()
        report["runs_rules_missed"] = missed_runs_report(
            context,
            alerts.scope.startswith("case "),
            read_ledger(since_days),
            lambda start, end: data.get_findings(
                limit=FINDINGS_LIMIT, timestamp_start=start, timestamp_end=end
            ),
            rules_by_technique,
            since_days,
        )
        return report

    async def get_coverage_stats(self, source_type: Optional[str] = None) -> Dict:
        """
        Get overall coverage statistics.

        Args:
            source_type: Optional filter by source

        Returns:
            Coverage statistics
        """
        counts = catalog.rule_counts()
        techniques = catalog.techniques_per_language()
        by_source = {
            source: {"count": count, "techniques": techniques.get(source, 0)}
            for source, count in counts.items()
        }
        if source_type and source_type in by_source:
            return {"source": source_type, **by_source[source_type]}
        return {
            "total_detections": sum(counts.values()),
            "techniques_covered": len(catalog.rules_by_technique()),
            "by_source": by_source,
        }

    async def lint_detections(
        self,
        rule_yaml: Optional[str] = None,
        source_path: Optional[str] = None,
    ) -> Dict:
        """Reject Sigma rules keyed to a specific IP, host, user, or subnet.

        Pass a rule YAML string or a directory of ``.yml`` files. Does not
        run on ``add_source`` / clone; community corpora stay importable.
        """
        return lint_sigma(rule_yaml=rule_yaml, source_path=source_path)

    async def check_detection_candidate(
        self,
        rule_yaml: Optional[str] = None,
        events: Optional[List[Dict]] = None,
        technique_id: Optional[str] = None,
        hostname: Optional[str] = None,
        started_at: Optional[str] = None,
        ended_at: Optional[str] = None,
        ruled_out_events: Optional[List[Dict]] = None,
        **_kwargs: object,
    ) -> Dict:
        """Lint a candidate Sigma rule and replay it against events already in hand.

        ``candidate`` is set only when lint passed, a proven event matched and no
        ruled-out event matched. The missed step stays inside ``candidate`` so the
        return is not an attack step.
        """
        lint = lint_sigma(rule_yaml=rule_yaml)
        replay = _replay_sigma(rule_yaml or "", events, ruled_out_events)
        candidate = None
        if (
            lint.get("passed")
            and replay.get("matched")
            and not replay.get("ruled_out_matched")
        ):
            candidate = {
                "technique_id": technique_id,
                "hostname": hostname,
                "started_at": started_at,
                "ended_at": ended_at,
                "rule_yaml": rule_yaml,
            }
        return {"lint": lint, "replay": replay, "candidate": candidate}

    async def reconstruct_run(self, steps: List[Dict], **_kwargs: object) -> Dict:
        """Correlate an action trace to ingested Findings; return per-step verdicts.

        Extra kwargs (including ``limit`` injected by ``/internal/tools/invoke``)
        are ignored so this does not freeze a later execute-tool JSON shape.
        """
        span = span_for_steps(steps)
        findings = DatabaseDataService().get_findings(
            limit=10000,
            timestamp_start=span[0] if span else None,
            timestamp_end=span[1] if span else None,
        )
        return reconstruct(steps, findings)


# Global instance for reuse
_security_detection_tools = None


def get_security_detection_tools() -> SecurityDetectionsTools:
    """Get or create global instance"""
    global _security_detection_tools
    if _security_detection_tools is None:
        _security_detection_tools = SecurityDetectionsTools()
    return _security_detection_tools


# Sigma's single-event detection: named selections joined by a boolean condition.
# Anything outside it raises _Unevaluable, which the replay reports and never passes.

Matcher = Callable[[Dict[str, Any]], bool]

_MODIFIERS = {"contains", "startswith", "endswith", "all", "re"}
_RE_FLAGS = {"i": re.IGNORECASE, "m": re.MULTILINE, "s": re.DOTALL}
_RE_MAX_PATTERN = 256
_RE_MAX_VALUE = 4096
# Worst-case steps a |re check may risk: len(value) ** (unbounded + 1) * bounded choices.
_RE_STEP_BUDGET = 10**9


class _Unevaluable(Exception):
    """A construct the replay cannot evaluate; the message names it."""


def _replay_sigma(
    rule_yaml: str, events: Any, ruled_out_events: Any = None
) -> Dict[str, Any]:
    if ruled_out_events is None:
        ruled_out_events = []
    if not isinstance(events, list):
        return _replay(False, False, "events must be a list")
    if not isinstance(ruled_out_events, list):
        return _replay(False, False, "ruled_out_events must be a list")
    try:
        rule = _compile_rule(rule_yaml)
        matched = any(isinstance(e, dict) and rule(e) for e in events)
        ruled_out = next(
            (e for e in ruled_out_events if isinstance(e, dict) and rule(e)), None
        )
    except _Unevaluable as exc:
        return _replay(False, False, str(exc))
    except (RecursionError, ValueError) as exc:
        # Deeply nested conditions, or numbers too long to print.
        return _replay(False, False, f"rule could not be replayed: {exc}")
    if ruled_out is not None:
        reason = "a ruled-out event matched"
    elif matched:
        reason = None
    else:
        reason = "no event matched the detection"
    return _replay(True, matched, reason, ruled_out)


def _replay(
    evaluated: bool,
    matched: bool,
    reason: Optional[str],
    ruled_out_event: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    return {
        "evaluated": evaluated,
        "matched": matched,
        "reason": reason,
        "ruled_out_matched": ruled_out_event is not None,
        "ruled_out_event": ruled_out_event,
    }


def _compile_rule(rule_yaml: str) -> Matcher:
    try:
        docs = [d for d in yaml.safe_load_all(rule_yaml) if d is not None]
    except yaml.YAMLError as exc:
        problem = getattr(exc, "problem", None) or "parse error"
        raise _Unevaluable(f"rule is not valid YAML: {problem}") from None
    if len(docs) != 1:
        raise _Unevaluable(
            "rule has %d YAML documents; correlation and meta rules are not replayed"
            % len(docs)
        )
    rule = docs[0]
    if not isinstance(rule, dict):
        raise _Unevaluable("rule is not a YAML mapping")
    if "correlation" in rule:
        raise _Unevaluable("correlation rules are not replayed")
    detection = rule.get("detection")
    if not isinstance(detection, dict):
        raise _Unevaluable("rule has no detection mapping")
    if "timeframe" in detection:
        raise _Unevaluable("timeframe needs an aggregation; it is not replayed")
    condition = detection.get("condition")
    if isinstance(condition, list) and condition:
        condition = " or ".join(f"({c})" for c in condition)
    if not isinstance(condition, str) or not condition.strip():
        raise _Unevaluable("detection has no condition")
    if "|" in condition:
        raise _Unevaluable(f"aggregation in condition is not replayed: {condition!r}")
    selections = {
        str(name): _compile_selection(str(name), body)
        for name, body in detection.items()
        if name != "condition"
    }
    return _ConditionParser(condition, selections).parse()


def _compile_selection(name: str, body: Any) -> Matcher:
    if isinstance(body, dict):
        if not body:
            raise _Unevaluable(f"selection {name!r} is empty")
        clauses = [_compile_field(str(k), v) for k, v in body.items()]
        return lambda e: all(c(e) for c in clauses)
    if isinstance(body, (str, int, float)) and not isinstance(body, bool):
        body = [body]
    if isinstance(body, list) and body:
        if all(isinstance(b, dict) for b in body):
            maps = [_compile_selection(name, b) for b in body]
            return lambda e: any(m(e) for m in maps)
        if all(
            isinstance(b, (str, int, float)) and not isinstance(b, bool) for b in body
        ):
            # Keywords: any string value anywhere in the event contains one of them.
            globs = [[_STAR, *_wild_tokens(str(b)), _STAR] for b in body]
            return lambda e: any(_wild_match(g, v) for v in _strings(e) for g in globs)
    raise _Unevaluable(f"selection {name!r} is not a map, list of maps or keywords")


def _strings(value: Any):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for v in value.values():
            yield from _strings(v)
    elif isinstance(value, list):
        for v in value:
            yield from _strings(v)


def _compile_field(key: str, value: Any) -> Matcher:
    field, *mods = key.split("|")
    if not field:
        raise _Unevaluable(f"field {key!r} has no name")
    unknown = [m for m in mods if m not in _MODIFIERS and m not in _RE_FLAGS]
    if unknown or ("re" not in mods and any(m in _RE_FLAGS for m in mods)):
        bad = unknown or [m for m in mods if m in _RE_FLAGS]
        raise _Unevaluable(f"modifier {bad[0]!r} on {key!r} is not replayed")
    values = value if isinstance(value, list) else [value]
    if not values:
        raise _Unevaluable(f"field {key!r} has an empty value list")
    if value is None or (None in values and not mods):
        # null: the key is absent or None.
        rest = [_value_test(key, mods, v) for v in values if v is not None]
        return lambda e: e.get(field) is None or any(t(e.get(field)) for t in rest)
    tests = [_value_test(key, mods, v) for v in values]
    combine = all if "all" in mods else any

    def match(event: Dict[str, Any]) -> bool:
        if field not in event:
            return False
        return combine(t(event[field]) for t in tests)

    return match


def _value_test(key: str, mods: List[str], expected: Any) -> Callable[[Any], bool]:
    if expected is None or isinstance(expected, (dict, list)):
        raise _Unevaluable(f"value {expected!r} on {key!r} is not replayed")
    if "re" in mods:
        if not isinstance(expected, str):
            raise _Unevaluable(f"|re value on {key!r} is not a string")
        if set(mods) & {"contains", "startswith", "endswith"}:
            raise _Unevaluable(f"|re with another match modifier on {key!r}")
        return _re_test(key, expected, mods)
    if isinstance(expected, bool):
        if mods != [] and mods != ["all"]:
            raise _Unevaluable(f"boolean value on {key!r} takes no modifier")
        # bool is an int, so True == 1. A bool matches only a bool.
        return lambda a: isinstance(a, bool) and a == expected
    tokens = _wild_tokens(str(expected))
    if "contains" in mods or "endswith" in mods:
        tokens.insert(0, _STAR)
    if "contains" in mods or "startswith" in mods:
        tokens.append(_STAR)
    number = isinstance(expected, (int, float)) and not mods

    def test(actual: Any) -> bool:
        if isinstance(actual, bool):
            return False
        if number and isinstance(actual, (int, float)):
            return actual == expected
        if isinstance(actual, (int, float)):
            actual = str(actual)
        return isinstance(actual, str) and _wild_match(tokens, actual)

    return test


_STAR, _ANY = object(), object()


def _wild_tokens(value: str) -> List[Any]:
    """Sigma plain value: * and ? are wildcards; \\* \\? \\\\ are literal."""
    tokens: List[Any] = []
    i = 0
    while i < len(value):
        ch = value[i]
        if ch == "\\" and i + 1 < len(value) and value[i + 1] in "*?\\":
            tokens.extend(value[i + 1])
            i += 2
            continue
        if ch == "*":
            tokens.append(_STAR)
        elif ch == "?":
            tokens.append(_ANY)
        else:
            tokens.extend(ch.lower())
        i += 1
    return tokens


def _wild_match(tokens: List[Any], value: str) -> bool:
    """Case-insensitive full match; backtracks only to the last *, so O(len * tokens)."""
    text = value.lower()
    p = i = 0
    star, mark = -1, 0
    while i < len(text):
        if p < len(tokens) and tokens[p] is _STAR:
            star, mark, p = p, i, p + 1
        elif p < len(tokens) and (tokens[p] is _ANY or tokens[p] == text[i]):
            p, i = p + 1, i + 1
        elif star >= 0:
            mark += 1
            p, i = star + 1, mark
        else:
            return False
    while p < len(tokens) and tokens[p] is _STAR:
        p += 1
    return p == len(tokens)


def _re_test(key: str, pattern: str, mods: List[str]) -> Callable[[Any], bool]:
    if len(pattern) > _RE_MAX_PATTERN:
        raise _Unevaluable(
            f"|re pattern on {key!r} is longer than {_RE_MAX_PATTERN} characters"
        )
    unbounded, choices = _re_cost(key, pattern)
    flags = 0
    for m in mods:
        flags |= _RE_FLAGS.get(m, 0)
    try:
        compiled = re.compile(pattern, flags)
    except re.error as exc:
        raise _Unevaluable(f"|re pattern on {key!r} does not compile: {exc}") from None

    def test(actual: Any) -> bool:
        if isinstance(actual, bool):
            return False
        if isinstance(actual, (int, float)):
            actual = str(actual)
        if not isinstance(actual, str):
            return False
        n = len(actual)
        if n > _RE_MAX_VALUE or n ** (unbounded + 1) * choices > _RE_STEP_BUDGET:
            raise _Unevaluable(
                f"|re on {key!r} against a {n}-character value "
                "could backtrack too long; not replayed"
            )
        return compiled.search(actual) is not None

    return test


_QUANT = re.compile(r"\*|\+|\?|\{(\d*)(,?)(\d*)\}")


def _re_cost(key: str, pattern: str) -> tuple:
    """Bound a pattern's backtracking: (unbounded quantifiers, product of bounded ones).

    Python's re has no timeout, so a repeated group holding a quantifier or an
    alternation, and backreferences, are refused rather than risked.
    """
    unsafe = _Unevaluable(f"|re pattern on {key!r} could backtrack without bound")
    unbounded, choices, i, in_class = 0, 1, 0, False
    groups: List[bool] = []  # per open group: does its body repeat or branch
    risky_group = after_quant = False
    while i < len(pattern):
        ch = pattern[i]
        quant = None if in_class else _QUANT.match(pattern, i)
        if ch == "\\":
            if i + 1 < len(pattern) and pattern[i + 1].isdigit():
                raise unsafe
            i += 2
        elif in_class:
            in_class = ch != "]"
            i += 1
        elif quant and ch == "?" and after_quant:
            i += 1  # lazy marker, not a quantifier
        elif quant:
            if quant.group(0) == "*" or quant.group(0) == "+":
                unbounded += 1
            elif ch == "?":
                choices *= 2
            elif quant.group(2) and not quant.group(3):
                unbounded += 1
            elif quant.group(2):
                low = int(quant.group(1) or 0)
                choices *= max(1, int(quant.group(3)) - low + 1)
            if risky_group and ch != "?":
                raise unsafe
            if groups:
                groups[-1] = True
            i = quant.end()
            risky_group, after_quant = False, True
            continue
        elif ch == "(":
            groups.append(False)
            i += 2 if pattern[i + 1 : i + 2] == "?" else 1
        elif ch == ")":
            risky_group = groups.pop() if groups else False
            if groups and risky_group:
                groups[-1] = True
            i += 1
            after_quant = False
            continue
        else:
            if ch == "|" and groups:
                groups[-1] = True
            in_class = ch == "["
            i += 1
        risky_group = after_quant = False
    if choices > _RE_STEP_BUDGET:
        raise unsafe
    return unbounded, choices


class _ConditionParser:
    """Recursive descent over: or < and < not < ( ) | N of pattern | name."""

    _TOKEN = re.compile(r"\s*(\(|\)|[^\s()]+)")

    def __init__(self, condition: str, selections: Dict[str, Matcher]):
        self.condition = condition
        self.selections = selections
        self.tokens = [m.group(1) for m in self._TOKEN.finditer(condition)]
        self.pos = 0

    def parse(self) -> Matcher:
        matcher = self._or()
        if self.pos != len(self.tokens):
            self._fail(f"unexpected {self.tokens[self.pos]!r}")
        return matcher

    def _fail(self, what: str) -> NoReturn:
        raise _Unevaluable(f"condition {self.condition!r}: {what}")

    def _peek(self) -> Optional[str]:
        return self.tokens[self.pos] if self.pos < len(self.tokens) else None

    def _next(self) -> str:
        token = self._peek()
        if token is None:
            self._fail("ends early")
        self.pos += 1
        return token

    def _or(self) -> Matcher:
        parts = [self._and()]
        while (self._peek() or "").lower() == "or":
            self.pos += 1
            parts.append(self._and())
        return parts[0] if len(parts) == 1 else (lambda e: any(p(e) for p in parts))

    def _and(self) -> Matcher:
        parts = [self._not()]
        while (self._peek() or "").lower() == "and":
            self.pos += 1
            parts.append(self._not())
        return parts[0] if len(parts) == 1 else (lambda e: all(p(e) for p in parts))

    def _not(self) -> Matcher:
        if (self._peek() or "").lower() == "not":
            self.pos += 1
            inner = self._not()
            return lambda e: not inner(e)
        return self._atom()

    def _atom(self) -> Matcher:
        token = self._next()
        if token == "(":
            inner = self._or()
            if self._next() != ")":
                self._fail("unbalanced brackets")
            return inner
        if (self._peek() or "").lower() == "of":
            self.pos += 1
            return self._of(token.lower(), self._next())
        if token in self.selections:
            return self.selections[token]
        self._fail(f"names unknown selection {token!r}")

    def _of(self, quantity: str, target: str) -> Matcher:
        if quantity not in ("1", "any", "all"):
            self._fail(f"'{quantity} of' is not replayed")
        if target == "them":
            names = [n for n in self.selections if not n.startswith("_")]
        else:
            names = [n for n in self.selections if fnmatch.fnmatchcase(n, target)]
        if not names:
            self._fail(f"{target!r} names no selection")
        parts = [self.selections[n] for n in names]
        combine = all if quantity == "all" else any
        return lambda e: combine(p(e) for p in parts)
