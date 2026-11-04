"""
Security-Detections tool integration - Pure Python implementation
Answers from the detection rules every configured repo holds (Sigma, Splunk, Elastic, KQL),
as core/detections/inventory syncs them into Postgres.
"""

from typing import Any, Dict, List, Optional

import yaml

from core.agents.projections import read_projection
from core.detections.gaps import gap_report, read_alert_labels
from core.detections.inventory import catalog
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
        self, query: str, source_type: Optional[str] = None, limit: int = 20
    ) -> List[Dict]:
        """
        Search across all detection rules using keywords.

        Args:
            query: Search query (e.g., "powershell base64", "lateral movement")
            source_type: Optional filter by source ("sigma", "splunk", "elastic", "kql")
            limit: Maximum number of results

        Returns:
            List of matching detection rules
        """
        return catalog.search(query, source_type, limit)

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

    async def identify_gaps(self, context: str = "") -> Dict:
        """Techniques seen on stored alerts that no active rule declares, with what it cannot say.

        Args:
            context: A case id, an ATT&CK label to match, or empty for every stored alert.
        """
        return gap_report(
            context,
            read_alert_labels(context),
            catalog.rules_by_technique(),
            catalog.rules_by_tactic(),
            catalog.rule_counts(),
        )

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
        **_kwargs: object,
    ) -> Dict:
        """Lint a candidate Sigma rule and replay it against events already in hand.

        ``candidate`` is set only when lint passed and one event matched. The
        missed step stays inside ``candidate`` so the return is not an attack step.
        """
        lint = lint_sigma(rule_yaml=rule_yaml)
        replay = _replay_sigma(rule_yaml or "", events)
        candidate = None
        if lint.get("passed") and replay.get("matched"):
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


def _replay_sigma(rule_yaml: str, events: Any) -> Dict[str, Any]:
    """One selection of scalar equality and ``|contains``, AND-ed across fields."""
    if not isinstance(events, list):
        return _replay(False, False, "events must be a list")
    fields = _selection_fields(rule_yaml)
    if fields is None:
        return _replay(
            False,
            False,
            "replay only evaluates one selection of equality and |contains",
        )
    matched = any(
        isinstance(event, dict) and _event_matches(event, fields) for event in events
    )
    if matched:
        return _replay(True, True, None)
    return _replay(True, False, "no event matched the selection")


def _replay(evaluated: bool, matched: bool, reason: Optional[str]) -> Dict[str, Any]:
    return {"evaluated": evaluated, "matched": matched, "reason": reason}


def _selection_fields(rule_yaml: str) -> Optional[List[tuple]]:
    try:
        parsed = yaml.safe_load(rule_yaml)
    except yaml.YAMLError:
        return None
    if not isinstance(parsed, dict):
        return None
    detection = parsed.get("detection")
    if not isinstance(detection, dict):
        return None
    selections = {key: value for key, value in detection.items() if key != "condition"}
    if len(selections) != 1:
        return None
    name, body = next(iter(selections.items()))
    condition = detection.get("condition")
    if not isinstance(condition, str) or condition.strip() != name:
        return None
    if not isinstance(body, dict) or not body:
        return None
    fields: List[tuple] = []
    for key, value in body.items():
        field = _field_clause(str(key), value)
        if field is None:
            return None
        fields.append(field)
    return fields


def _field_clause(key: str, value: Any) -> Optional[tuple]:
    parts = key.split("|")
    if len(parts) == 1:
        op = "eq"
    elif len(parts) == 2 and parts[1] == "contains":
        op = "contains"
    else:
        return None
    name = parts[0]
    if not name:
        return None
    if op == "contains":
        if not isinstance(value, str):
            return None
        return name, op, value
    if isinstance(value, bool) or isinstance(value, (str, int, float)):
        return name, op, value
    return None


def _event_matches(event: Dict[str, Any], fields: List[tuple]) -> bool:
    for name, op, expected in fields:
        if name not in event:
            return False
        actual = event[name]
        if op == "eq":
            # bool is an int, so True == 1. A scalar match is the value as written.
            if (
                isinstance(actual, bool) != isinstance(expected, bool)
                or actual != expected
            ):
                return False
            continue
        if not isinstance(actual, str) or expected not in actual:
            return False
    return True
