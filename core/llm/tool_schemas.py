"""
Claude API Tool Schemas
Defines all tools available to Claude via function calling
"""

# The recall tool's signature is declared with the rest of the recall contract
# (#729) rather than transcribed here: a schema that drifts from the handler's
# arguments fails as "no history", which reads as an entity nobody has looked at.
from core.cases.case_status import CASE_STATUSES
from core.memory.recall_contract import RECALL_PARAMETERS, RECALL_TOOL
from core.skills.skill_library import READ_SKILL_TOOL

# Customer-specific exceptions per copy (#3794): kept apart from the rule body.
_EXCEPTION_ITEMS = {
    "type": "array",
    "items": {
        "type": "object",
        "properties": {
            "match": {
                "type": "object",
                "description": "{field: value} in this copy's own field names; every pair must equal. A CIDR value matches an address inside it.",
            },
            "note": {"type": "string", "description": "Why it is expected. Required."},
        },
        "required": ["match", "note"],
    },
}
_EXCEPTIONS_SCHEMA = {
    "type": "object",
    "description": "Customer-specific exceptions per copy, stored apart from the rule body instead of a `not filter_*` naming a host, address or account (lint refuses those). Keys are the copies being saved. Vigil's runner drops hits an exception matches; the replay counts a ruled-out event a sigma exception matches as excluded (a proven event one matches refuses the save). Adds no rule version; an exception already on the copy with the same match is kept as is.",
    "properties": {
        "splunk": _EXCEPTION_ITEMS,
        "elastic": _EXCEPTION_ITEMS,
        "sigma": _EXCEPTION_ITEMS,
    },
}

# Security-Detections Tools (Core functionality)
SECURITY_DETECTION_TOOLS = [
    {
        "name": "analyze_coverage",
        "description": "Analyze detection coverage for MITRE ATT&CK techniques, or report per-technique layer verdicts for a sanctioned run. Pass techniques for catalog counts (Sigma / Splunk / Elastic / KQL), with by_tool: active rules per tool ('repo', or a SIEM: 'elastic' for Kibana, 'splunk' for Splunk saved searches, 'wazuh') and, for a SIEM, its Runs, Sees, Quiet, Accurate, Noisy, Silent and Caught pass / fail / unknown / unchecked counts (Sees: is the data the rule reads arriving; Quiet: does the rule stay under its SIEM's alerts-per-day threshold; Accurate: are at least 10% of its 20+ judged alerts real; Noisy: did today's alerts jump far above the rule's own daily mean; Silent: did a rule that fired regularly stop firing; Caught: did this version alert on a real attack step from a compose run or proven incident in 30 days, on a technique it declares. Caught only passes: its unchecked count is the untested rules, never a fail), and never_firing: its rules with no alert in 30 days; repo_deployed: per SIEM, the repo rules' linked copies counted by state (pass / fail / unknown / unchecked / disabled) and the repo rules with no copy there ('not deployed'). Kibana, Wazuh and Splunk rules get Sees (a Wazuh rule's data source is its decoder, read from the Wazuh indexer behind elastic-siem; a Splunk saved search's is its index/sourcetype or data model). Kibana, Wazuh and Splunk rules get Quiet (a Wazuh rule's alerts counted on wazuh-alerts-* on the Wazuh indexer behind elastic-siem; a Splunk saved search's are its notables under Enterprise Security, else its triggered alerts, else its scheduled runs that fired an action other than Splunk's data-output ones (summary index, lookup, telemetry), so an untracked alert counts too); Kibana, Wazuh and Splunk rules also get Accurate and Caught (a Splunk notable is traced to its saved search by search_name and the app and owner in its orig_sid). Kibana, Wazuh and Splunk rules get Noisy, Silent and never_firing (a Wazuh rule below level 3 with no alerts, or a Splunk scheduled report (not tracked, not a correlation search, no action but data-output ones such as outputtelemetry or summary_index), cannot alert, so it passes both and is not counted as never-firing; a Wazuh rule below level 3 that has alerts is judged on them). Pass run_id or an action-trace steps list for the run report: reconstruct on read, group by technique_id, verdicts rule | loglm | both | missed. Do not mix the two.",
        "input_schema": {
            "type": "object",
            "properties": {
                "techniques": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "List of MITRE technique IDs (e.g., ['T1059.001', 'T1071.001']). Catalog path; omit when reporting a run.",
                },
                "run_id": {
                    "type": "string",
                    "description": "Agent run id. Asks the agent layer for journaled execute traces, then reconstructs. Report path, not catalog counts.",
                },
                "steps": {
                    "type": "array",
                    "items": {"type": "object"},
                    "description": "Action-trace steps (same shape as reconstruct_run). Report path without a ledger.",
                },
            },
        },
    },
    {
        "name": "search_detections",
        "description": "Search the detection rules of every configured repo (Sigma, Splunk, Elastic, KQL), Vigil's own overlay of drafted rules, and the rules Kibana's detection engine, Splunk's scheduled and correlation saved searches and the Wazuh manager run, using keywords. Each hit carries its tool ('overlay', 'repo', 'elastic', 'splunk' or 'wazuh'), its status ('active', or 'disabled' for a Kibana rule or Splunk saved search switched off or a Wazuh rule excluded in ossec.conf) and checks.runs: whether the SIEM runs it ({result: pass|fail|unknown, reason, checked_at}; reason holds the SIEM's own error), and checks.sees: whether the data it reads is arriving ({result, reason, data_source, checked_at}; for Kibana, fail names the index with no events in 7d or the required fields missing from its mapping, unknown for ES|QL, ML or an unreadable source; for Wazuh, data_source is the rule's decoder ('wazuh decoder sshd'), fail means the Wazuh archive is on and no event was decoded as it in 7d, unknown when only alerts are kept and none came from it or when the indexer is not read; for Splunk, data_source is 'index=X sourcetype=Y' or 'datamodel DM.Dataset' read from the search's first clause after macro expansion, fail means no events there in 7d, unknown for a lookup, | rest, subsearch, eventtype, real-time search, undefined macro or a data model not accelerated), and checks.quiet: whether its alert volume stays under the SIEM's quiet_max_alerts_per_day ({result, reason, count, days, checked_at}; count is its alerts over the last days, from its version's first sight and at most 30; fail when count / days is over the threshold, e.g. 'fail: 4,210 alerts in 30 d (140/day), over 50/day'; unknown 'version too new' under 24 h, 'not counted this sync', or the search error), checks.accurate: the share of the alerts of this version judged real ({result, reason, real, judged, analyst, agent, checked_at}; each alert's latest analyst verdict wins over the agent's, from noise marks and closed Cases; fewer than 20 judged is unknown, under 10% real is fail; reason e.g. 'fail: 3 of 42 real (analyst 9, agent 33)'), checks.noisy ({result, reason, checked_at}; fail when today's alerts (UTC day so far) reach noisy_min_alerts and noisy_multiple times the rule's daily mean over the 29 days before) and checks.silent ({result, reason, days_fired, checked_at}; fail when the rule had no alert in the last silent_days after firing on at least silent_min_active_days of the days before in 30; a rule that rarely fires is never silent; days_fired 0 means it never fired in 30 days); noisy and silent are unknown when the rule has been in the inventory under 7 d, was not counted, or the search failed; and checks.caught: whether this version alerted on a real attack step ({result: pass, reason, checked_at}; a cited Kibana, Wazuh or Splunk alert of this version correlated with a finished compose run's step or a proven hunt or Case Verdict in the last 30 days, on a technique the rule declares, compared at parent level; reason e.g. 'pass: caught in Case 412 (proven, analyst), T1059.001, 2026-10-30'). Caught only ever passes and is never cleared on that version: no checks.caught means untested, not failed. Filter tool='elastic', 'splunk' or 'wazuh' with runs='fail' to answer which of that SIEM's rules don't run and why, sees='fail' for which are starved of data, accurate='fail' for which mostly fire on nothing, caught='untested' for which no attack run or proven incident has shown alerting, or quiet='fail' for which would flood analysts (a Splunk search's alerts are its notables under Enterprise Security, else its triggered alerts, else its scheduled runs that fired an action other than Splunk's data-output ones). A loaded Wazuh rule's Runs is pass ('loaded by the manager'); Kibana, Wazuh and Splunk rules get Noisy and Silent (a Wazuh rule below level 3 with no alerts passes both, as it cannot alert, and one with alerts is judged on them; a Splunk scheduled report (not tracked, not a correlation search, no action but data-output ones such as outputtelemetry or summary_index) reads both pass, 'a scheduled report; it does not alert'). A Kibana or Splunk copy linked to a repo rule also carries checks.drift ({result, reason, checked_at}): fail when the copy was disabled, removed or edited in the SIEM while its repo rule stayed the same (the reason names the versions and when each was first seen), pass when it matches, the repo is ahead, or the repo rule is not active. A repo or overlay hit carries deployed: per SIEM, the copies linked to it (each with its state: pass / fail / unknown / unchecked / disabled, its runs, its quiet check or null, its accurate and its caught) or 'not deployed'. A SIEM hit carries origin: its link to a repo or overlay rule (origin_rule_id, kind same_id | known_name | agent | declined, by, reason, at) or null. An overlay hit also carries stage (its current rollout stage: shadow, live or off; null for a Sigma copy) and stages (every stage row, oldest first: version, stage, proof, actor, reason, at). Each hit has flagged: true when its latest Runs, Sees, Quiet, Accurate, Noisy, Silent or drift change went pass -> fail (it broke or drifted and has not recovered); filter flagged=true for those. When exactly one rule matches, its hit carries recent_changes: its last 10 recorded check changes (check runs|sees|quiet|accurate|noisy|silent|caught|drift|status, old -> new, changed_at, version, reason), newest first.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Search query - keywords or phrases (e.g., 'powershell base64', 'lateral movement', 'mimikatz'). Empty matches every rule the filters keep.",
                },
                "source_type": {
                    "type": "string",
                    "enum": ["sigma", "splunk", "elastic", "kql", "wazuh"],
                    "description": "Optional: Filter by detection format. 'elastic' covers repo tomls and Kibana rules alike; 'splunk' covers repo ESCU rules and Splunk saved searches. Omit to search all formats.",
                },
                "tool": {
                    "type": "string",
                    "enum": ["overlay", "repo", "elastic", "splunk", "wazuh"],
                    "description": "Optional: Filter by where the rule was read: Vigil's overlay, a configured repo, Kibana's detection engine, Splunk's saved searches, or the Wazuh manager.",
                },
                "runs": {
                    "type": "string",
                    "enum": ["pass", "fail", "unknown"],
                    "description": "Optional: Keep rules whose latest version has this Runs result. Only SIEM rules have one.",
                },
                "stage": {
                    "type": "string",
                    "enum": ["shadow", "live", "off"],
                    "description": "Optional: Keep rules whose current rollout stage is this. Only overlay rules have one.",
                },
                "sees": {
                    "type": "string",
                    "enum": ["pass", "fail", "unknown"],
                    "description": "Optional: Keep rules whose latest version has this Sees result. Only SIEM rules have one.",
                },
                "quiet": {
                    "type": "string",
                    "enum": ["pass", "fail", "unknown"],
                    "description": "Optional: Keep rules whose latest version has this Quiet result; fail is a rule averaging more alerts per day than its SIEM's threshold. Kibana, Wazuh and Splunk rules have one.",
                },
                "accurate": {
                    "type": "string",
                    "enum": ["pass", "fail", "unknown"],
                    "description": "Optional: Keep rules whose latest version has this Accurate result. Kibana, Wazuh and Splunk rules have one.",
                },
                "caught": {
                    "type": "string",
                    "enum": ["pass", "untested"],
                    "description": "Optional: 'pass' keeps rules whose latest version alerted on a real attack step (a compose run or proven incident); 'untested' keeps those with no Caught result. Caught never fails. Kibana, Wazuh and Splunk rules get one.",
                },
                "flagged": {
                    "type": "boolean",
                    "description": "Optional: true keeps rules whose Runs, Sees, Quiet, Accurate or drift went pass -> fail and have not recovered; false keeps the rest.",
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximum number of results to return (default: 20)",
                    "default": 20,
                },
            },
            "required": [],
        },
    },
    {
        "name": "link_detection_rule",
        "description": "Record which repo rule a SIEM rule (a copy in Kibana or Splunk) came from. The inventory sync links a copy by itself only when certain: same_id (a Kibana rule with a repo Elastic rule's rule_id) or known_name (a Splunk search named 'ESCU - <name> - Rule' and the one repo Splunk rule named <name>); use this for the rest, after finding the candidates with search_detections. action 'link' sets the copy's origin; 'decline' records that the copy is not from that repo rule, so the sync never links it; 'unlink' clears a link or decline, handing the copy back to the sync. Every call is written to the audit log with you as the actor and your reason. Rule ids are search_detections' rule_id.",
        "input_schema": {
            "type": "object",
            "properties": {
                "copy_rule_id": {
                    "type": "integer",
                    "description": "rule_id of the SIEM rule (a hit whose tool is 'elastic').",
                },
                "action": {
                    "type": "string",
                    "enum": ["link", "unlink", "decline"],
                },
                "origin_rule_id": {
                    "type": "integer",
                    "description": "rule_id of the repo rule (tool 'repo' or 'overlay'). Required to link or decline; optional to unlink.",
                },
                "reason": {
                    "type": "string",
                    "description": "Why. Required, and written to the audit log.",
                },
            },
            "required": ["copy_rule_id", "action", "reason"],
        },
    },
    {
        "name": "identify_gaps",
        "description": "Compare the ATT&CK techniques seen on stored alerts with the detection rules actually loaded, and list techniques no rule declares. covered_on_paper_only lists techniques whose SIEM rules (Kibana, Splunk and Wazuh) each fail Runs or Sees (each technique row has siem_runs and siem_sees). blind_spots lists each data source (Kibana index patterns, a Wazuh decoder, or a Splunk 'index=X sourcetype=Y' / 'datamodel DM.Dataset') with no events in 7d or missing fields, and the active SIEM rules it starves, each with its reason. Reads stored data only and writes nothing. Read answer and unknown first: alerts that name a tactic and no technique cannot prove a gap, and answer is unknown when the data cannot say. Kibana, Wazuh and Splunk rules get a Sees check. runs_rules_missed lists proven hunts and closed Cases (Verdicts from the last since_days) whose hosts, IPs and users drew no rule alert in their window (LogLM-only counts as missed), grouped by technique. Each group has its runs, how many a person confirmed, the active rules declaring the technique, and ruled_out: disproven / false_positive runs on it that a draft rule must not catch. Only groups with 2+ runs or 1+ confirmed are listed; the rest are counted. A run with no host, IP or user, or whose Findings read was cut off, is in its unknown, never missed. rule_health lists the open flags: SIEM rules (and Vigil's Elastic and Splunk overlay copies, whose Runs is Vigil's own run of the query and whose Sees covers a copy at stage shadow or live that no SIEM copy has taken over) whose latest Runs, Sees, Quiet, Accurate, Noisy, Silent or drift change went pass -> fail (a check that never passed is no flag; Quiet fails when the rule goes over its alerts-per-day threshold; Accurate when under 10% of its 20+ judged alerts are real; Noisy (Kibana, Wazuh and Splunk rules) when today's alerts jump far above its own daily mean; Silent (likewise) when a rule that fired regularly has had no alert for a week; drift fails when a linked Kibana or Splunk copy was disabled, removed or edited in the SIEM while its repo rule stayed the same, and stays flagged on a removed copy), each with rule_id, name, tool, status, check, old, new, since, version and reason; a flag clears when that check passes again. opened and cleared count flags opened and cleared in the last 7 days. rule_health is not scoped by context.",
        "input_schema": {
            "type": "object",
            "properties": {
                "context": {
                    "type": "string",
                    "description": "A case id, an ATT&CK label to match on stored alerts (e.g. 'T1021', 'lateral movement'), or empty for every stored alert. A threat name such as 'APT29' is not mapped to techniques: pass the techniques it uses as techniques. For runs_rules_missed a case id gives that Case's run and a label its technique.",
                },
                "since_days": {
                    "type": "integer",
                    "description": "How many days of hunt and Case Verdicts runs_rules_missed reads (default: 30).",
                    "default": 30,
                },
                "techniques": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "ATT&CK technique ids from any source, e.g. the attack patterns (x_mitre_id) a threat intel intrusion set uses or a report names. Each is listed in techniques_checked.rows with its bucket: proven (an active SIEM rule passes both Runs and Sees on the same rule), siem_unproven (SIEM rules exist, none passes both), repo_only (only repo or overlay rules declare it) or none. related_rules counts rules on its parent or subtechniques; they never make it proven. techniques_checked.gaps lists every id that is not proven. Ids that are not technique ids are in rejected and unknown.",
                },
            },
            "required": [],
        },
    },
    {
        "name": "get_coverage_stats",
        "description": "Get overall detection coverage statistics including total detections, techniques covered, and breakdown by source format. Use this for high-level coverage overview.",
        "input_schema": {
            "type": "object",
            "properties": {
                "source_type": {
                    "type": "string",
                    "enum": ["sigma", "splunk", "elastic", "kql", "wazuh"],
                    "description": "Optional: Get stats for specific source format only",
                }
            },
        },
    },
    {
        "name": "get_detection_count",
        "description": "Get count of detection rules, optionally filtered by source format.",
        "input_schema": {
            "type": "object",
            "properties": {
                "source_type": {
                    "type": "string",
                    "enum": ["sigma", "splunk", "elastic", "kql", "wazuh"],
                    "description": "Optional: Count for specific source format",
                }
            },
        },
    },
    {
        "name": "lint_detections",
        "description": "Lint Sigma detection rules for match keys tied to a specific IP, hostname, user, or subnet. Returns rewrite guidance to make the rule behavioural. Pass rule_yaml for one rule or source_path to walk .yml files under an existing source. Does not block importing community rule sources.",
        "input_schema": {
            "type": "object",
            "properties": {
                "rule_yaml": {
                    "type": "string",
                    "description": "Sigma rule YAML to lint. Pass this or source_path, not both.",
                },
                "source_path": {
                    "type": "string",
                    "description": "Directory of Sigma .yml files (or a single .yml file) to lint.",
                },
            },
        },
    },
    {
        "name": "check_detection_candidate",
        "description": "Lint a candidate Sigma rule and replay its single-event detection against events the caller already has: named selections, conditions with and/or/not, brackets, 1 of/all of, null and keyword selections, the modifiers contains, startswith, endswith, all and re, case-insensitive with * and ? wildcards. Aggregations (| count()), correlation rules and unknown modifiers return replay.evaluated false with the reason. Pass ruled_out_events too: the draft must stay quiet on them. Returns lint, replay, and candidate; candidate is set only when lint passed, a proven event matched and no ruled-out event matched. Does not query Findings or a SIEM and does not write a rule.",
        "input_schema": {
            "type": "object",
            "properties": {
                "rule_yaml": {
                    "type": "string",
                    "description": "Sigma rule YAML to lint and replay.",
                },
                "events": {
                    "type": "array",
                    "items": {"type": "object"},
                    "description": "Proven events for the missed step, fetched from the SIEM for its window. Field names are matched to event keys as written.",
                },
                "ruled_out_events": {
                    "type": "array",
                    "items": {"type": "object"},
                    "description": "Events from runs whose explanation was ruled out. The candidate fails if the rule matches any of them.",
                },
                "technique_id": {
                    "type": "string",
                    "description": "Technique id of the missed step this candidate answers.",
                },
                "hostname": {
                    "type": "string",
                    "description": "Hostname of the missed step.",
                },
                "started_at": {
                    "type": "string",
                    "description": "Window start of the missed step.",
                },
                "ended_at": {
                    "type": "string",
                    "description": "Window end of the missed step.",
                },
            },
            "required": [
                "rule_yaml",
                "events",
                "technique_id",
                "hostname",
                "started_at",
                "ended_at",
            ],
        },
    },
    {
        "name": "save_detection_rule",
        "description": "Save one drafted or changed detection rule into Vigil's overlay (a local rule source Vigil owns), then sync it into the rule inventory. Pass a Splunk copy (ESCU-style YAML with search), an Elastic copy (detection-rules TOML with a [rule] table), or both, plus an optional Sigma form. Each copy is written as its own file. A copy that does not parse, or that fails lint, refuses the whole save and writes nothing: the Sigma form and each Splunk search and Elastic query are linted for a literal IP, CIDR, environment host (.corp, .local, .internal, .lan) or named person account, even under not (a whole private range such as 10.0.0.0/8 is not one environment and passes). Pass proven_events (and ruled_out_events) to have the Sigma form replayed on them first: the save is refused, with the replay in the error, unless the Sigma form is given, can be replayed, matches a proven event and matches no ruled-out event. The result is stored as checks.replay on the Sigma copy's version (result not_run when no events are passed). A copy keeps the id it carries (ESCU id, Elastic rule_id, Sigma id): pass an existing rule's id to change that rule (a logic change is a new version); omit it for a new rule. Returns each saved copy's rule_id, id, version and stage: a new Splunk or Elastic rule or version enters shadow (nobody sees its alerts); a Sigma copy has no stage. Vigil runs the Elastic copy itself, so its [rule] language must be eql, esql or lucene (kuery is refused) and an eql or lucene copy needs an index. Pass exceptions to keep an expected host, address or account out of the rule body. Writes no SIEM.",
        "input_schema": {
            "type": "object",
            "properties": {
                "splunk_yaml": {
                    "type": "string",
                    "description": "Splunk copy: ESCU-style YAML with name, search and optionally id, mitre_attack_id, description.",
                },
                "elastic_toml": {
                    "type": "string",
                    "description": "Elastic copy: detection-rules TOML with a [rule] table (name, query, type, language, optionally rule_id).",
                },
                "sigma_yaml": {
                    "type": "string",
                    "description": "Optional Sigma form of the same rule, with a detection block; linted before saving. Required when events are passed.",
                },
                "proven_events": {
                    "type": "array",
                    "items": {"type": "object"},
                    "description": "Proven events the rule must catch, fetched from the SIEM for the missed step's window. The Sigma form must match at least one.",
                },
                "ruled_out_events": {
                    "type": "array",
                    "items": {"type": "object"},
                    "description": "Events from runs whose explanation was ruled out. The Sigma form must match none of them, except those a sigma exception matches.",
                },
                "exceptions": _EXCEPTIONS_SCHEMA,
            },
            "required": [],
        },
    },
    {
        "name": "detection_rule_exception",
        "description": "Add a customer-specific exception to one of Vigil's overlay copies (search_detections tool 'overlay'), or remove one. An exception keeps an expected host, address or account out of the rule's query: Vigil's runner drops the hits whose fields equal every pair of match (a CIDR value matches an address inside it) before they are counted or become findings, and counts its matches per day in the version's checks.exceptions. Field names are the copy's own: ECS paths for the Elastic copy, Splunk field names for the Splunk copy, Sigma fields for the Sigma form. Each exception covers one copy. Adding or removing one makes no new rule version and does not restart shadow. Exceptions are listed in the deploy pull request's description and stop applying once the SIEM's own copy runs the rule. Every addition and removal is recorded with you as the actor; a removed exception is kept with who removed it, when and why.",
        "input_schema": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["add", "remove"]},
                "rule_id": {
                    "type": "integer",
                    "description": "The overlay copy's rule_id. Required to add.",
                },
                "match": {
                    "type": "object",
                    "description": "{field: value}; every pair must equal the hit's field. Required to add.",
                },
                "note": {
                    "type": "string",
                    "description": "Why these hits are expected here. Required to add.",
                },
                "exception_id": {
                    "type": "integer",
                    "description": "The exception to remove. Required to remove.",
                },
                "reason": {
                    "type": "string",
                    "description": "Why it is removed. Required to remove.",
                },
            },
            "required": ["action"],
        },
    },
    {
        "name": "reconstruct_run",
        "description": "Reconstruct a red-run action trace into per-step detection verdicts. Correlates each step to ingested Findings by host, entity, and time. Verdict is rule, loglm, both, or missed. Cite matching Finding ids. Unknown keys on a step are ignored.",
        "input_schema": {
            "type": "object",
            "properties": {
                "steps": {
                    "type": "array",
                    "items": {"type": "object"},
                    "description": "Action-trace steps. Join keys are host/entity/time (hostname, src_ip, user, started_at/ended_at or timestamp). Unknown keys are ignored.",
                }
            },
            "required": ["steps"],
        },
    },
]

# DeepTempo Findings Tools (Already implemented in backend)
DEEPTEMPO_FINDING_TOOLS = [
    {
        "name": "list_findings",
        "description": "List security findings with server-side pagination and compact summaries. Each summary names the hosts, users and indicators the finding carries; use get_finding for the finding's triage and a sample of its source records. Supports filtering by severity, data_source, state, and pagination via offset/limit. Each summary carries the finding's terminal state (e.g. waiting, working, resolved_auto, dismissed).",
        "input_schema": {
            "type": "object",
            "properties": {
                "severity": {
                    "type": "string",
                    "enum": ["low", "medium", "high", "critical"],
                    "description": "Filter by severity level",
                },
                "data_source": {
                    "type": "string",
                    "description": "Filter by data source (e.g., 'sysmon', 'cloudtrail')",
                },
                "state": {
                    "type": "string",
                    "enum": ["open", "dismissed", "resolved"],
                    "description": "Filter by the finding's derived state: open (not dismissed and in no closed case), dismissed, or resolved (in a closed case). Omit for all.",
                },
                "cluster_id": {
                    "type": "string",
                    "description": "Filter by cluster id",
                },
                "min_anomaly_score": {
                    "type": "number",
                    "description": "Minimum anomaly score, inclusive",
                },
                "sort_by": {
                    "type": "string",
                    "enum": ["timestamp", "anomaly_score", "severity"],
                    "description": "Column to sort by",
                    "default": "timestamp",
                },
                "sort_order": {
                    "type": "string",
                    "enum": ["asc", "desc"],
                    "description": "Sort direction",
                    "default": "desc",
                },
                "offset": {
                    "type": "integer",
                    "description": "Pagination offset (0-based)",
                    "default": 0,
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximum number of findings to return (at most 25; a page can stop short so it fits, so read returned and next_offset)",
                    "default": 20,
                    "minimum": 1,
                    "maximum": 25,
                },
            },
        },
    },
    {
        "name": "search_findings",
        "description": 'Search findings by text query across finding IDs, descriptions, and entity context. Returns counts over every match (total, by_severity, by_data_source, by_state as open/dismissed/resolved, and the top 10 by_source_ip / by_destination_ip, with *_omitted giving how many more addresses were left out), then one page of compact summaries, each naming the hosts, users and indicators the finding carries. Answer count questions ("how many findings involve X, by severity?") from those counts in one call with a low limit; do not page through results to count them. Use get_finding for the triage and a sample of the source records behind a specific result.',
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": 'Text search across finding IDs, descriptions and entity context. Terms split on whitespace or commas; a finding must match every term (e.g. "10.0.0.1 10.0.0.2" finds findings naming both hosts).',
                },
                "severity": {
                    "type": "string",
                    "enum": ["low", "medium", "high", "critical"],
                    "description": "Filter by severity level",
                },
                "data_source": {
                    "type": "string",
                    "description": "Filter by data source",
                },
                "state": {
                    "type": "string",
                    "enum": ["open", "dismissed", "resolved"],
                    "description": "Filter by the finding's derived state: open (not dismissed and in no closed case), dismissed, or resolved (in a closed case). Omit for all.",
                },
                "sort_by": {
                    "type": "string",
                    "enum": ["timestamp", "anomaly_score", "severity"],
                    "default": "anomaly_score",
                },
                "sort_order": {
                    "type": "string",
                    "enum": ["asc", "desc"],
                    "default": "desc",
                },
                "offset": {"type": "integer", "default": 0},
                "limit": {
                    "type": "integer",
                    "default": 20,
                    "minimum": 1,
                    "maximum": 25,
                    "description": "Page size, at most 25. A page can stop short so it fits; returned, has_more and next_offset describe what came back.",
                },
            },
            "required": ["query"],
        },
    },
    {
        "name": "get_findings_stats",
        "description": "Get aggregate statistics about all findings without returning individual finding data. Returns counts by severity, data source and state (open, dismissed, resolved). Takes no filters: for counts over a subset, use search_findings. Use this to get an overview before drilling into specific findings.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_finding",
        "description": "Get detailed information about a specific finding by ID. Returns the finding's title, severity, data source, scores and predicted techniques first, then the daemon's stored triage under triage (severity, confidence, category, recommended_action, reasoning) when there is one, then entity_context with every entity it names. The raw source record under entity_context.source_evidence is a sample of its records (records_shown says how many of how many); for a NetFlow finding entity_context.netflow_digest counts every flow by host pair. A null confidence in mitre_predictions means the source named the technique and gave no confidence: it is unknown, not 0. detection_rule names the inventory rule and version that fired it (rule_id, name, version, query, first_seen, checks such as runs), or resolved false with the reason it is unknown; only Kibana rules resolve today.",
        "input_schema": {
            "type": "object",
            "properties": {
                "finding_id": {
                    "type": "string",
                    "description": "The finding ID (e.g., 'f-20260209-001')",
                }
            },
            "required": ["finding_id"],
        },
    },
    {
        "name": "dismiss_finding",
        "description": "Dismiss a finding as noise. This is the finding's terminal state: it reads Dropped and leaves the Overview feed. Recorded with you as the actor and the reason you give.",
        "input_schema": {
            "type": "object",
            "properties": {
                "finding_id": {
                    "type": "string",
                    "description": "The finding ID to dismiss",
                },
                "reason": {
                    "type": "string",
                    "description": "Why this finding is noise. Required.",
                },
            },
            "required": ["finding_id", "reason"],
        },
    },
    {
        "name": "list_cases",
        "description": "List investigation cases with optional filters. Returns active and closed cases.",
        "input_schema": {
            "type": "object",
            "properties": {
                "status": {
                    "type": "string",
                    "enum": list(CASE_STATUSES),
                    "description": "Filter by case status",
                },
                "severity": {
                    "type": "string",
                    "enum": ["low", "medium", "high", "critical"],
                    "description": "Filter by severity (cases store this as priority)",
                },
                "priority": {
                    "type": "string",
                    "enum": ["low", "medium", "high", "critical"],
                    "description": "Filter by priority",
                },
                "limit": {"type": "integer", "default": 50},
            },
        },
    },
    {
        "name": "get_case",
        "description": "Get detailed information about a specific case including all findings, timeline, activities, and MITRE techniques. A null confidence in mitre_predictions means the source named the technique and gave no confidence: it is unknown, not 0.",
        "input_schema": {
            "type": "object",
            "properties": {"case_id": {"type": "string", "description": "The case ID"}},
            "required": ["case_id"],
        },
    },
    {
        "name": "case_records",
        "description": (
            "Read the case records held for an investigation: its tasks and "
            "escalations, and a page of the findings attached to the case, newest "
            "first, each a summary (id, title, severity, source, time, the hosts "
            "and users it names), with the total the case holds. Page with next_offset "
            "while has_more is true. get_finding returns one finding with its triage and a sample of its source records. "
            "When the case holds network-flow findings, flows sums every flow "
            "of all of them per host pair (not just this page): flows, first "
            "and last seen, distinct destination ports, answered flows (any "
            "bytes or packets back), bytes and packets each way, top servers. "
            "no_digest names flow findings whose flows it does not count; "
            "unknown counts flows that lacked a byte or packet field."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "case_id": {
                    "type": "string",
                    "description": "The case whose records to read.",
                },
                "offset": {
                    "type": "integer",
                    "default": 0,
                    "description": "Findings to skip, to read the next page.",
                },
                "limit": {
                    "type": "integer",
                    "description": "Most findings to return on this page.",
                },
            },
            "required": ["case_id"],
        },
    },
    {
        "name": "read_case_evidence",
        "description": (
            "Read the evidence attached to a case, in full. With case_id alone, "
            "list its evidence (id, type, name, description, size, sha256, who "
            "collected it and when). With evidence_id, return a window of that "
            "item's text from offset (characters) for length characters, with total_chars "
            "and next_offset to read on. With query, return the lines that "
            "contain it (case-insensitive) with their line numbers and the "
            "offset that opens a window there, capped, with how many were "
            "omitted. A document condensed before a hunt read it is attached "
            "here as the original."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "case_id": {
                    "type": "string",
                    "description": "The case whose evidence to read.",
                },
                "evidence_id": {
                    "type": "integer",
                    "description": "One evidence item from the case's list.",
                },
                "offset": {
                    "type": "integer",
                    "default": 0,
                    "description": "Character to start the window at.",
                },
                "length": {
                    "type": "integer",
                    "default": 8000,
                    "description": "Most characters to return (at most 16000).",
                },
                "query": {
                    "type": "string",
                    "description": "Text to find in the item, instead of a window.",
                },
            },
            "required": ["case_id"],
        },
    },
    {
        "name": "create_case",
        "description": "Create a new investigation case. Use this to organize related findings into a case for tracking and investigation.",
        "input_schema": {
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "Case title/summary"},
                "description": {
                    "type": "string",
                    "description": "Detailed case description",
                },
                "severity": {
                    "type": "string",
                    "enum": ["low", "medium", "high", "critical"],
                    "description": "Case severity. Used as priority when priority is omitted.",
                },
                "priority": {
                    "type": "string",
                    "enum": ["low", "medium", "high", "critical"],
                    "description": "Case priority. Wins over severity when both are set.",
                },
                "status": {
                    "type": "string",
                    "enum": list(CASE_STATUSES),
                    "description": "Initial status",
                    "default": "new",
                },
                "assignee": {"type": "string", "description": "Assignee"},
                "tags": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Tags applied after the case is created",
                },
                "finding_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional: Initial findings to add to case",
                },
            },
            "required": ["title"],
        },
    },
    {
        "name": "add_finding_to_case",
        "description": "Add a finding to an existing case.",
        "input_schema": {
            "type": "object",
            "properties": {
                "case_id": {"type": "string"},
                "finding_id": {"type": "string"},
            },
            "required": ["case_id", "finding_id"],
        },
    },
    {
        "name": "update_case",
        "description": "Update an existing case's title, description, status, or priority.",
        "input_schema": {
            "type": "object",
            "properties": {
                "case_id": {"type": "string", "description": "The case ID to update"},
                "title": {"type": "string", "description": "New title"},
                "description": {
                    "type": "string",
                    "description": "New description or executive summary",
                },
                "status": {
                    "type": "string",
                    "enum": list(CASE_STATUSES),
                },
                "priority": {
                    "type": "string",
                    "enum": ["low", "medium", "high", "critical"],
                },
                "assignee": {"type": "string", "description": "Assignee"},
                "add_note": {
                    "type": "string",
                    "description": "Note appended to the case",
                },
            },
            "required": ["case_id"],
        },
    },
    {
        "name": "add_resolution_step",
        "description": "Add a resolution step to a case documenting a containment, eradication, or recovery action taken or recommended.",
        "input_schema": {
            "type": "object",
            "properties": {
                "case_id": {"type": "string", "description": "The case ID"},
                "description": {
                    "type": "string",
                    "description": "What needs to be done or was done",
                },
                "action_taken": {
                    "type": "string",
                    "description": "The specific action taken or recommended",
                },
                "result": {
                    "type": "string",
                    "description": "Outcome or expected outcome of the action",
                },
            },
            "required": ["case_id", "description", "action_taken"],
        },
    },
    {
        "name": "list_completed_hunts",
        "description": (
            "Return completed threat-hunt runs that finished in an "
            "assessment window. Each hunt is the existing hunt projection: "
            "hypotheses (with provenance), evidence provenance, verdict, "
            "checkpoint resolutions (approver identity), and timestamps. "
            "Hunts whose projection is not yet available are omitted."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "start": {
                    "type": "string",
                    "description": "Window start (ISO-8601 timestamp)",
                },
                "end": {
                    "type": "string",
                    "description": "Window end (ISO-8601 timestamp)",
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximum number of completed hunts to return",
                    "default": 200,
                },
            },
            "required": ["start", "end"],
        },
    },
    {
        "name": "list_learning_episodes",
        "description": (
            "What Vigil learned in a window: one episode per concluded hunt or "
            "case, read from distilled memory, and a finished compose run whose "
            "projection has an execute trace. Each carries kind, "
            "investigation_id, origin_run_id (null for a case), concluded_at, "
            "and a payload of the verdicts (with source stances) and gaps that "
            "investigation recorded. An investigation that concluded nothing is "
            "still an episode. Read-only."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "start": {
                    "type": "string",
                    "description": "Window start (ISO-8601 timestamp or date)",
                },
                "end": {
                    "type": "string",
                    "description": "Window end (ISO-8601 timestamp or date, inclusive)",
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximum number of episodes to return, newest first",
                    "default": 200,
                },
            },
            "required": ["start", "end"],
        },
    },
    {
        "name": "export_learning_episodes",
        "description": (
            "Write a chosen set of learning episodes to a JSONL file in the "
            "exports directory, one episode per line, selected by "
            "{kind, investigation_id} pairs from list_learning_episodes. A "
            "finished compose run with an execute trace is included as kind "
            "emulation. By default entity values are redacted to their type "
            "(ip:*), and host, ip, user, and command are dropped from missed "
            "emulation steps; pass identified=true to keep them. An empty "
            "selection writes no file."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "episodes": {
                    "type": "array",
                    "description": "Episodes to export, as returned by list_learning_episodes",
                    "items": {
                        "type": "object",
                        "properties": {
                            "kind": {
                                "type": "string",
                                "enum": ["hunt", "case", "emulation"],
                            },
                            "investigation_id": {"type": "string"},
                        },
                        "required": ["kind", "investigation_id"],
                    },
                },
                "identified": {
                    "type": "boolean",
                    "description": "Keep entity values instead of redacting them to their type",
                    "default": False,
                },
                "name": {
                    "type": "string",
                    "description": "File stem under the exports directory; defaults to a timestamped name",
                },
            },
            "required": ["episodes"],
        },
    },
    {
        "name": "replay_hunt",
        "description": (
            "Replay a completed threat hunt: for each decision the hunt lead "
            "took, the digest it was shown is rebuilt from the ledger and "
            "compared with the one recorded (rebuilt, recorded, mismatch), "
            "alongside what memory recalled for the hunt. Answers a bounded "
            "summary, not the digests: pass step (or from_step and to_step) "
            "for the steps you need, or decision_id for one decision; with "
            "none it answers the latest few steps. What a run is doing now, "
            "or why it stopped, is in the case brief, not here."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "run_id": {
                    "type": "string",
                    "description": "Workflow run id of the hunt (a uuid)",
                },
                "decision_id": {
                    "type": "string",
                    "description": "Replay only this decision",
                },
                "step": {
                    "type": "integer",
                    "description": "Replay only this step (iteration)",
                },
                "from_step": {
                    "type": "integer",
                    "description": "First step of a range",
                },
                "to_step": {
                    "type": "integer",
                    "description": "Last step of a range",
                },
            },
            "required": ["run_id"],
        },
    },
]

# Attack Layer Tools
ATTACK_LAYER_TOOLS = [
    {
        "name": "get_technique_rollup",
        "description": "Get rollup statistics for MITRE techniques showing finding counts and severity distribution. min_confidence applies only to techniques with a confidence: at 0 an occurrence with no confidence is counted, above 0 it is excluded and counted in excluded_occurrences on the response.",
        "input_schema": {
            "type": "object",
            "properties": {
                "min_confidence": {
                    "type": "number",
                    "description": "Minimum technique confidence to count",
                    "default": 0.0,
                },
                "time_range": {
                    "type": "string",
                    "enum": ["24h", "7d", "30d", "all"],
                    "description": "Window over finding timestamps",
                    "default": "all",
                },
            },
        },
    },
]

# Approval Tools
APPROVAL_TOOLS = [
    {
        "name": "list_pending_approvals",
        "description": "List pending actions awaiting approval. Returns actions that require analyst approval before execution (e.g., host isolation, IP blocking).",
        "input_schema": {
            "type": "object",
            "properties": {
                "limit": {
                    "type": "integer",
                    "description": "Maximum number of pending actions to return",
                    "default": 50,
                }
            },
        },
    },
    {
        "name": "get_approval_action",
        "description": "Get detailed information about a specific pending action by ID.",
        "input_schema": {
            "type": "object",
            "properties": {
                "action_id": {
                    "type": "string",
                    "description": "The action ID (e.g., 'action-20260209-001')",
                }
            },
            "required": ["action_id"],
        },
    },
    {
        "name": "approve_action",
        "description": "Does not approve. Leaves the action as it is and tells the caller to confirm it in Approvals.",
        "input_schema": {
            "type": "object",
            "properties": {
                "action_id": {
                    "type": "string",
                    "description": "The action ID to approve",
                },
            },
            "required": ["action_id"],
        },
    },
    {
        "name": "reject_action",
        "description": "Reject a pending action. The action will not be executed and will be marked as rejected.",
        "input_schema": {
            "type": "object",
            "properties": {
                "action_id": {
                    "type": "string",
                    "description": "The action ID to reject",
                },
                "reason": {"type": "string", "description": "Reason for rejection"},
            },
            "required": ["action_id", "reason"],
        },
    },
    {
        "name": "get_approval_stats",
        "description": "Get statistics about approval actions including total, pending, approved, rejected, and executed counts.",
        "input_schema": {"type": "object", "properties": {}},
    },
]

# The local indicator database the threat-feed poller fills. Present whether or
# not a deployment carries an external intel integration, which is why it is here
# rather than left to MCP.
THREAT_INTEL_TOOLS = [
    {
        "name": "lookup_indicators",
        "description": (
            "Look up observables in Vigil's threat-indicator database, populated "
            "from the configured threat feeds. Returns one row per value asked "
            "about, with known=false for any the feeds do not carry -- an "
            "indicator no feed knows is a finding about the indicator."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "indicator_type": {
                    "type": "string",
                    "description": "What kind of observable these are",
                    "enum": ["ip", "domain", "url", "md5", "sha1", "sha256"],
                    "default": "ip",
                },
                "values": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "The observables to look up, in one batch",
                },
            },
            "required": ["values"],
        },
    },
    {
        "name": "propose_feed_hunts",
        "description": (
            "Which recently fed threat indicators has nobody hunted? Reads the "
            "newest rows of Vigil's threat-indicator database and runs each "
            "through the same coverage check as `check_hunt_coverage`. Returns "
            "one entry per uncovered indicator with its Entity Key, the feed row "
            "(type, value, source, last_seen) and a `proposal` body for "
            "POST /api/workflows/threat-hunt/execute; indicators already "
            "`running` or `concluded` are counted and omitted. Read-only: this "
            "never starts a hunt."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "limit": {
                    "type": "integer",
                    "description": (
                        "How many of the most recent indicators to classify "
                        "(capped at 200)"
                    ),
                    "default": 200,
                },
            },
        },
    },
]

# Episodic memory (#732). Reading it is a backend tool rather than an MCP server
# because the rows are Vigil's own: a deployment that has run a hunt has memory
# to read, and one that has not gets empty lists.
MEMORY_TOOLS = [
    {
        "name": RECALL_TOOL,
        "description": (
            "Recall what past investigations saw and concluded about an entity. "
            "Returns the Sightings (what was observed, per investigation and "
            "source), the Verdicts (what was concluded, with the stance of each "
            "corroborating source) and the Declared Gaps (questions nobody "
            "gathered evidence for) for each Entity Key given as `type:value`. "
            "An entity nobody has investigated returns empty lists, which is an "
            "answer and not an error. Results are capped per key and overall; "
            "`dropped` says what was left out and `ranking` says on what basis. "
            "Pass your own role in `caller_kind` and `caller_id`: every read is "
            "logged for audit, and one that does not say who asked is logged as "
            "`unknown`."
        ),
        "input_schema": RECALL_PARAMETERS,
    },
    {
        "name": "check_hunt_coverage",
        "description": (
            "Before proposing a hunt from a threat report, ask whether one already "
            "covers it. Pass the report text (STIX bundle or plain text) and/or "
            "already-extracted Entity Keys (`type:value`) and ATT&CK technique ids. "
            "Answers `running` (a hunt is on it now; extend that run via "
            "POST /api/agent-runs/{run_id}/directives kind `extend`), `concluded` "
            "(a past hunt reached a Verdict; rows carry outcome, date and "
            "origin_run_id) or `uncovered`. The last two include a `proposal` body "
            "for POST /api/workflows/threat-hunt/execute. Matched and unmatched "
            "keys and techniques are listed. Read-only: this never starts a hunt."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "report": {
                    "type": "string",
                    "description": "The report: a STIX 2.x bundle or plain text",
                },
                "entity_keys": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Entity Keys already extracted, as `type:value`",
                },
                "techniques": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "ATT&CK technique ids already extracted (T1566)",
                },
            },
        },
    },
]

# Agent skills (#925). One static tool for every skill rather than one per
# skill: the library is a set of documents, and the prompt lists which exist.
SKILL_TOOLS = [
    {
        "name": READ_SKILL_TOOL,
        "description": (
            "Read a skill from the library listed in <available_skills>. Without "
            "`file` this returns the skill's SKILL.md body: the procedure to follow. "
            "A body may point at supporting files (references/, assets/); pass one "
            "as `file`, a path relative to the skill directory, to read it. Text "
            "only, nothing is executed, and a path outside the skill is refused."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Skill name exactly as listed in <available_skills>",
                },
                "file": {
                    "type": "string",
                    "description": (
                        "Optional file inside the skill directory, e.g. "
                        "references/checklist.md. Omit for the SKILL.md body."
                    ),
                },
            },
            "required": ["name"],
        },
    },
]

# Combine all tools
ALL_TOOLS = (
    SECURITY_DETECTION_TOOLS
    + DEEPTEMPO_FINDING_TOOLS
    + ATTACK_LAYER_TOOLS
    + THREAT_INTEL_TOOLS
    + APPROVAL_TOOLS
    + MEMORY_TOOLS
    + SKILL_TOOLS
)

# Chat's door to the connected MCP integrations (#1959). Declared in place of
# every integration's own schemas, which outgrew what a provider will take, and
# kept out of ALL_TOOLS: hunts curate their integrations in playbook_resolver.
FIND_INTEGRATION_TOOLS = "find_integration_tools"
CALL_INTEGRATION_TOOL = "call_integration_tool"

INTEGRATION_TOOLS = [
    {
        "name": FIND_INTEGRATION_TOOLS,
        "description": (
            "Search the tools of the connected integrations (the servers named in "
            "the system prompt) by keyword. Returns at most 8 matches, each with "
            "its full name, description and input schema. Call this before "
            "call_integration_tool, and pass a matching tool's name to it."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "What the tool should do, e.g. 'sigma rules for T1059' or 'ip reputation'",
                },
                "server": {
                    "type": "string",
                    "description": "Optional integration server name to search within, e.g. splunk-selfhosted",
                },
            },
            "required": ["query"],
        },
    },
    {
        "name": CALL_INTEGRATION_TOOL,
        "description": (
            "Run one connected integration tool that find_integration_tools "
            "returned. Pass its exact name and arguments matching its input schema."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "The tool's full name as find_integration_tools returned it",
                },
                "arguments": {
                    "type": "object",
                    "description": "The tool's arguments, shaped by its input schema",
                },
            },
            "required": ["name"],
        },
    },
]
