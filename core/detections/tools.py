"""
Security-Detections tool integration - Pure Python implementation
Answers from the detection rules every configured repo holds (Sigma, Splunk, Elastic, KQL)
and the rules Kibana's detection engine, Splunk's saved searches and the Wazuh
manager run, as core/detections/inventory syncs them into Postgres.
"""

import asyncio
from typing import Any, Dict, List, Optional

from core.agents.projections import read_projection
from core.detections.exceptions import edit_exception
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
from core.detections.overlay import save_rule
from core.detections.reconstruction import (
    coverage_report,
    reconstruct,
    span_for_steps,
    steps_from_dispatch_results,
)
from core.detections.replay import replay_sigma
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

        Catalog counts carry by_tool: each SIEM's Runs, Sees, Quiet, Noisy and
        Silent results tallied and its never-firing rules, and repo_deployed: the repo rules' linked copies by state.

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
        stage: Optional[str] = None,
        sees: Optional[str] = None,
        flagged: Optional[bool] = None,
        quiet: Optional[str] = None,
        accurate: Optional[str] = None,
        caught: Optional[str] = None,
    ) -> List[Dict]:
        """
        Search across all detection rules using keywords.

        Args:
            query: Search query (e.g., "powershell base64", "lateral movement")
            source_type: Optional filter by rule format ("sigma", "splunk", "elastic", "kql", "wazuh")
            limit: Maximum number of results
            tool: Optional filter by where the rule was read ("overlay", "repo", "elastic", "splunk", "wazuh")
            runs: Optional filter by Runs result ("pass", "fail", "unknown")
            stage: Optional filter by current rollout stage ("shadow", "live", "off")
            sees: Optional filter by Sees result ("pass", "fail", "unknown")
            quiet: Optional filter by Quiet result ("pass", "fail", "unknown"):
                fail is a Kibana or Wazuh rule averaging over its alerts-per-day threshold
            flagged: Optional filter: rules with (True) or without (False) an open flag
            accurate: Optional filter by Accurate result ("pass", "fail", "unknown"):
                whether a Kibana, Wazuh or Splunk rule's judged alerts are mostly real
            caught: Optional filter: "pass" for versions that alerted on a real
                attack step, "untested" for the rest

        Returns:
            List of matching detection rules, each with its status, checks and
            whether it is flagged; an overlay rule also with its stage and stage
            rows; a single hit also carries its recent check changes
        """
        return catalog.search(
            query or "",
            source_type,
            limit,
            tool,
            runs,
            stage,
            sees,
            flagged,
            quiet,
            accurate,
            caught,
        )

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
            copy_rule_id: The SIEM rule's rule_id (a search_detections hit with tool 'elastic')
            action: 'link', 'unlink' or 'decline'
            reason: Why; required, and written to the audit log
            origin_rule_id: The repo or overlay rule's rule_id; required to link or decline
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
        and the proven hunts and closed Cases no rule alerted on (``runs_rules_missed``),
        and the SIEM rules that broke (``rule_health``: a Runs, Sees, Quiet, Noisy,
        Silent or drift check that went pass -> fail).

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
        report["rule_health"] = catalog.rule_health()
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
        replay = replay_sigma(rule_yaml or "", events, ruled_out_events)
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

    async def save_detection_rule(
        self,
        splunk_yaml: Optional[str] = None,
        elastic_toml: Optional[str] = None,
        sigma_yaml: Optional[str] = None,
        proven_events: Optional[List[Dict]] = None,
        ruled_out_events: Optional[List[Dict]] = None,
        exceptions: Optional[Dict[str, List[Dict]]] = None,
        **_kwargs: object,
    ) -> Dict:
        """Save one rule's copies into Vigil's overlay; each enters shadow.

        With events, the Sigma form is replayed on them first and the save is
        refused unless a proven event matches and no ruled-out one does (a
        ruled-out event a Sigma exception matches is excluded).
        """
        return await asyncio.to_thread(
            save_rule,
            splunk_yaml,
            elastic_toml,
            sigma_yaml,
            proven_events,
            ruled_out_events,
            exceptions,
        )

    async def detection_rule_exception(
        self,
        action: str = "",
        rule_id: Any = None,
        match: Any = None,
        note: Any = None,
        exception_id: Any = None,
        reason: Any = None,
        **_kwargs: object,
    ) -> Dict:
        """Add a customer-specific exception to an overlay copy, or remove one."""
        return await asyncio.to_thread(
            edit_exception, action, rule_id, match, note, exception_id, reason
        )

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
