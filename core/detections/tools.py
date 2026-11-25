"""
Security-Detections tool integration - Pure Python implementation
Answers from the detection rules every configured repo holds (Sigma, Splunk, Elastic, KQL)
and the rules Kibana's detection engine, Splunk's saved searches and the Wazuh
manager run, as core/detections/inventory syncs them into Postgres.
"""

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from core.agents.projections import read_projection
from core.detections.exceptions import edit_exception
from core.detections.gaps import (
    FINDINGS_LIMIT,
    gap_report,
    missed_runs_report,
    read_alert_labels,
    read_ledger,
    read_window_findings,
    technique_check,
)
from core.detections.inventory import catalog, coverage, metrics
from core.detections.inventory.accurate import FALSE_PAGE, judged_false_page
from core.detections.inventory.links import link_rule
from core.detections.lint import lint_sigma
from core.detections.overlay import save_rule
from core.detections.reconstruction import (
    EmulatedSteps,
    coverage_report,
    reconstruct,
    span_for_steps,
    steps_from_dispatch_results,
)
from core.detections.replay import replay_sigma
from core.storage.database_data_service import DatabaseDataService
from core.workflows.workflow_run_service import run_unreadable


def _name_rules(report: Dict[str, Any]) -> None:
    """Each step's rule-origin citations named by the version that fired them
    (``catalog.resolve_findings``), as ``rules`` on the step; LogLM and probe
    citations are never a rule."""
    from core.detections.inventory.caught import _SKIPPED, resolve_cited
    from core.storage.connection import get_db_manager

    steps = [s for t in report.get("techniques") or [] for s in t.get("steps") or []]
    cites = [
        c
        for s in steps
        for c in s.get("citations") or []
        if c.get("finding_id") and c.get("data_source") not in _SKIPPED
    ]
    if not cites:
        return
    with get_db_manager().session_scope() as session:
        resolved = resolve_cited(session, {c["finding_id"] for c in cites})
    for c in cites:
        r = resolved.get(c["finding_id"]) or {}
        if r.get("resolved"):
            c["rule"] = {
                k: r[k] for k in ("rule_id", "version", "name", "source", "native_key")
            }
        else:
            c["rule_unresolved"] = r.get("reason") or "the finding is not stored"
    for s in steps:
        named = {
            (c["rule"]["rule_id"], c["rule"]["version"]): c["rule"]
            for c in s.get("citations") or []
            if "rule" in c
        }
        s["rules"] = list(named.values())


async def _readable_emulations(
    since_days: int, since: Optional[datetime]
) -> List[Dict[str, Any]]:
    """Scored compose runs over the gaps window, less those on a Case the caller
    can't read."""
    from core.memory.learning_episodes import emulation_episodes

    end = datetime.now(timezone.utc)
    start = since or end - timedelta(days=since_days)
    episodes = await emulation_episodes(start, end)
    hidden = await asyncio.gather(
        *(asyncio.to_thread(run_unreadable, e["investigation_id"]) for e in episodes)
    )
    return [e for e, h in zip(episodes, hidden) if not h]


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
        read: Optional[EmulatedSteps] = None
        if isinstance(steps, list) and steps:
            trace: List = steps
        elif run_id:
            # A run on a case the caller can't read (#4359) reads as no run.
            hidden = await asyncio.to_thread(run_unreadable, run_id)
            projection = None if hidden else await read_projection(run_id)
            if projection is None:
                return {
                    "run_id": run_id,
                    "techniques": [],
                    "error": "no readable run",
                }
            read = steps_from_dispatch_results(projection)
            trace = read.steps
        else:
            trace = steps if isinstance(steps, list) else []
        reconstructed = await self.reconstruct_run(steps=trace)
        report = coverage_report(trace, reconstructed)
        if read is not None:
            # What the run's emulators reported that is not scored, beside the techniques.
            report.update(read.notes())
        await asyncio.to_thread(_name_rules, report)
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
        rule_id: Any = None,
        offset: int = 0,
        held: Optional[bool] = None,
    ) -> Any:
        """
        Search across all detection rules using keywords.

        Args:
            query: Search query (e.g., "powershell base64", "lateral movement")
            source_type: Optional filter by rule format ("sigma", "splunk", "elastic", "kql", "wazuh")
            limit: Maximum number of results. With rule_id, the judged-false page size (at most 25).
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
                attack step, "fail" for those a later emulation showed missing it,
                "untested" for those with no result
            rule_id: When set, return that one rule plus a page of its judged-false
                determinations. Without it the call stays a list and builds no page.
            offset: Where that page starts. Ignored without rule_id.
            held: Optional filter: repo rules the import check holds from
                coverage (True: checks.import pending, flagged or failed) or not

        Returns:
            List of matching detection rules, each with its status, checks and
            whether it is flagged and held; an overlay rule also with its stage and stage
            rows; a single hit also carries its recent check changes. With
            rule_id, that one rule plus ``judged_false``.
        """
        try:
            rid = _rule_id(rule_id)
        except ValueError:
            return {"error": "rule_id must be a detection rule id"}
        if rid is not None:
            hits = catalog.search("", None, 1, rule_id=rid)
            if not hits:
                return {"error": f"No detection rule {rid}"}
            return {
                **hits[0],
                "judged_false": judged_false_page(
                    rid, _offset(offset), _page_size(limit)
                ),
            }
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
            held=held,
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
        self,
        context: str = "",
        since_days: int = 30,
        techniques: Any = None,
        since: Optional[datetime] = None,
        **_kwargs: object,
    ) -> Dict:
        """Techniques seen on stored alerts that no active rule declares, with what it cannot say,
        and the proven hunts, closed Cases and emulated steps no rule alerted on
        (``runs_rules_missed``),
        and the SIEM rules that broke (``rule_health``: a Runs, Sees, Quiet, Noisy,
        Silent or drift check that went pass -> fail).

        Args:
            context: A case id, an ATT&CK label to match, or empty for every stored alert.
            since_days: How far back Verdicts are read for runs_rules_missed.
            techniques: ATT&CK ids from any source (e.g. what threat intel names);
                each is bucketed in ``techniques_checked``. An id that is not an
                active ATT&CK technique (made up, revoked or deprecated) is listed in
                ``techniques_checked.unknown`` with its reason, never as a gap.
            since: The daily gaps run's marker: only alerts stored, and proven
                Verdicts concluded, at or after it are counted. Not offered to the model.
        """
        # A model may send "30"; keep the window within what timedelta can hold.
        since_days = min(max(int(since_days), 0), 3650)
        if not isinstance(since, datetime):
            since = None
        alerts = read_alert_labels(context, since)
        rules_by_technique = catalog.rules_by_technique()
        siem_checks = catalog.siem_checks_by_technique()
        report = gap_report(
            context,
            alerts,
            rules_by_technique,
            catalog.rules_by_tactic(),
            catalog.rule_counts(),
            siem_checks,
            catalog.blind_spots(),
        )
        if techniques:
            if isinstance(techniques, str):
                techniques = techniques.replace(",", " ").split()
            checked = technique_check(techniques, rules_by_technique, siem_checks)
            report["techniques_checked"] = checked
            if checked["rejected"]:
                report["unknown"].append(
                    "Not ATT&CK technique ids, so not checked: "
                    + ", ".join(checked["rejected"])
                )
            if checked["unknown"]:
                report["unknown"].append(
                    "Not active ATT&CK techniques, so not checked: "
                    + ", ".join(
                        f"{u['technique']} ({u['reason']})" for u in checked["unknown"]
                    )
                )
            if report["rules_loaded"] and checked["gaps"]:
                report["answer"] = "gaps_found"
            elif (
                report["rules_loaded"]
                and checked["checked"]
                and report["answer"] == "unknown"
            ):
                report["answer"] = "no_gaps_found"
        report["runs_rules_missed"] = missed_runs_report(
            context,
            alerts.scope.startswith("case "),
            read_ledger(since_days),
            read_window_findings,
            rules_by_technique,
            since_days,
            since,
            await _readable_emulations(since_days, since),
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

    async def detection_metrics(self, **_kwargs: object) -> Dict:
        """The rollout's success metrics from stored data, each with its source."""
        return await asyncio.to_thread(metrics.report)

    async def emulate_next(self, **_kwargs: object) -> Dict:
        """The coverage map's techniques in the order to emulate next (no rule,
        unproven, proven with nothing caught, then the rest), as coverage()
        orders them. Says nothing about which tests an emulation runner holds."""
        report = await asyncio.to_thread(coverage.coverage)
        rows = {row["technique"]: row for row in report["techniques"]}
        keys = ("technique", "name", "tactic", "state", "rules", "caught")
        return {
            "techniques": [
                {k: rows[t][k] for k in keys} for t in report["emulate_next"]
            ],
            "summary": {**report["summary"], "caught": report["caught_techniques"]},
            "source": report["source"],
        }

    async def lint_detections(
        self,
        rule_yaml: Optional[str] = None,
        **_kwargs: object,
    ) -> Dict:
        """Reject a Sigma rule keyed to a specific IP, host, user, or subnet.

        Lints one rule YAML string; any other argument is ignored. Sync lints
        every imported repo and local rule the same way (checks.lint) without
        blocking it, and a failing one is left out of coverage counts.
        """
        if not (rule_yaml or "").strip():
            return {
                "error": "Pass rule_yaml: the Sigma rule YAML to lint",
                "passed": False,
            }
        return lint_sigma(rule_yaml=rule_yaml)

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
        wazuh_xml: Optional[str] = None,
        proven_event_ids: Optional[Dict[str, List[str]]] = None,
        attack_started_at: Optional[str] = None,
        attack_ended_at: Optional[str] = None,
        **_kwargs: object,
    ) -> Dict:
        """Save one rule's copies into Vigil's overlay; each Splunk and Elastic
        copy enters shadow (a Wazuh copy too: Vigil searches wazuh-archives-* for it in shadow).

        With events, the Sigma form is replayed on them first and the save is
        refused unless a proven event matches and no ruled-out one does (a
        ruled-out event a Sigma exception matches is excluded). With
        ``proven_event_ids``, Vigil reads the events from the SIEM and proves the
        Sigma form and each Splunk and Elastic copy on them (``save_proof``).
        """
        result = await asyncio.to_thread(
            save_rule,
            splunk_yaml,
            elastic_toml,
            sigma_yaml,
            proven_events,
            ruled_out_events,
            exceptions,
            wazuh_xml,
            proven_event_ids=proven_event_ids,
            attack_started_at=attack_started_at,
            attack_ended_at=attack_ended_at,
        )
        if result.get("gap_proposals"):
            result["gap_replays"] = list(
                await asyncio.gather(
                    *(_gap_replay(pid) for pid in result["gap_proposals"])
                )
            )
        return result

    async def detection_rule_exception(
        self,
        action: str = "",
        rule_id: Any = None,
        match: Any = None,
        note: Any = None,
        exception_id: Any = None,
        reason: Any = None,
        finding_ids: Any = None,
        **_kwargs: object,
    ) -> Dict:
        """Add a customer-specific exception to an overlay copy, or remove one."""
        return await asyncio.to_thread(
            edit_exception,
            action,
            rule_id,
            match,
            note,
            exception_id,
            reason,
            finding_ids,
        )

    async def reconstruct_run(self, steps: List[Dict], **_kwargs: object) -> Dict:
        """Correlate an action trace to ingested Findings; return per-step verdicts.

        Extra kwargs (including ``limit`` injected by ``/internal/tools/invoke``)
        are ignored so this does not freeze a later execute-tool JSON shape.
        """
        span = span_for_steps(steps)
        findings = DatabaseDataService().get_findings(
            limit=FINDINGS_LIMIT,
            timestamp_start=span[0] if span else None,
            timestamp_end=span[1] if span else None,
        )
        out = reconstruct(steps, findings)
        if len(findings) >= FINDINGS_LIMIT:
            # coverage_report reads a step no rule cited as unknown, not missed.
            out["cut_off"] = (
                f"the steps' window holds {FINDINGS_LIMIT}+ Findings and the read "
                "was cut off, so whether a rule alerted is unknown"
            )
        return out


# How long Vigil's replay of a gap proposal may hold up the save's answer.
GAP_REPLAY_TIMEOUT_S = 120


async def _gap_replay(proposal_id: str) -> Dict[str, Any]:
    """Lint and replay a gap proposal the save just attached to, as Vigil, on the
    attack its gap's hunts and Cases proved. A failure costs the replay, never the save.
    """
    from core.detections.engineering import proposals

    out: Dict[str, Any] = {"proposal_id": proposal_id}
    try:
        # Only a draft took this save's text on; one a person converted or rejected,
        # or one whose text this save left as it was, keeps its own checks.
        state = (await asyncio.to_thread(proposals.get, proposal_id))["state"]
        if state != "draft":
            out.update(
                state=state,
                skipped="this save did not change the proposal's rule text",
            )
            return out
        view = await asyncio.to_thread(
            proposals.lint, proposal_id, proposals.SYSTEM, proposals.SYSTEM
        )
        lint = (view["latest"].get("lint") or {}).get("evidence") or {}
        if not lint.get("passed"):
            out.update(state=view.get("state"), lint=lint)
            return out
        view = await asyncio.wait_for(
            proposals.replay(proposal_id, proposals.SYSTEM, kind=proposals.SYSTEM),
            GAP_REPLAY_TIMEOUT_S,
        )
        replayed = (view["latest"].get("replay") or {}).get("evidence") or {}
        out.update(
            state=view.get("state"),
            replay={
                k: replayed.get(k)
                for k in (
                    "attack_basis",
                    "proven",
                    "hits",
                    "attack_rows",
                    "false_positives",
                    "benign_rows",
                    "unproven_because",
                    "fields_absent",
                    "window",
                )
            },
        )
    except asyncio.TimeoutError:
        out["error"] = f"the replay did not finish within {GAP_REPLAY_TIMEOUT_S}s"
    except Exception as exc:  # noqa: BLE001 -- the save's answer stands
        out["error"] = str(exc) or type(exc).__name__
    return out


def _rule_id(value: Any) -> Optional[int]:
    """A detection rule id, or None when the argument was left out."""
    if value is None or value == "":
        return None
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise ValueError
    if isinstance(value, str) and not value.strip():
        return None
    try:
        rid = int(value)
    except ValueError:
        raise ValueError from None
    if rid < 1:
        raise ValueError
    return rid


def _offset(value: Any) -> int:
    try:
        start = int(value)
    except (TypeError, ValueError):
        return 0
    if isinstance(value, bool):
        return 0
    return max(start, 0)


def _page_size(limit: Any) -> int:
    """The judged-false page size: at least 1, at most FALSE_PAGE, else 20."""
    try:
        size = int(limit)
    except (TypeError, ValueError):
        return 20
    if isinstance(limit, bool):
        return 20
    return min(max(size, 1), FALSE_PAGE)


# Global instance for reuse
_security_detection_tools = None


def get_security_detection_tools() -> SecurityDetectionsTools:
    """Get or create global instance"""
    global _security_detection_tools
    if _security_detection_tools is None:
        _security_detection_tools = SecurityDetectionsTools()
    return _security_detection_tools
