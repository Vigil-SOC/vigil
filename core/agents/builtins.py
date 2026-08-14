"""Agent record type and built-in SOC agent definitions (Reorg R1 / #482).

Each built-in is a plain record shaped like a ``custom_agents`` row so
built-ins and customs build through the same path
(``core.agents.manager.SOCAgentLibrary.build_profile``). The old split
structures (``_BUILTIN_COMPONENT_CATEGORY`` and the task-routing keyword
table) are folded into each record as ``component_category`` and
``task_keywords``; the decision-log action id (GH #476) is folded in the
same way as ``decision_id``.
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import List, Optional


class AgentId(str, Enum):
    """Canonical id of every built-in agent — the *actor* vocabulary.

    Doubles as the ``id`` of the matching :data:`BUILTIN_AGENTS` record and
    the ``created_by`` value an agent stamps on records it authors. Custom
    agents are not members; they carry a ``custom-`` prefixed id from the
    database.
    """

    TRIAGE = "triage"
    INVESTIGATOR = "investigator"
    THREAT_HUNTER = "threat_hunter"
    CORRELATOR = "correlator"
    RESPONDER = "responder"
    REPORTER = "reporter"
    MITRE_ANALYST = "mitre_analyst"
    FORENSICS = "forensics"
    THREAT_INTEL = "threat_intel"
    COMPLIANCE = "compliance"
    MALWARE_ANALYST = "malware_analyst"
    NETWORK_ANALYST = "network_analyst"
    AUTO_RESPONDER = "auto_responder"


# The autonomous loop authors decisions of its own but is not an agent: it has
# no prompt, no BUILTIN_AGENTS record, and never appears in GET /agents.
ORCHESTRATOR_ACTOR = "orchestrator"
ORCHESTRATION_DECISION_ID = "orchestration"

# Fallback whenever a caller names no agent, and the landing agent for a new session.
DEFAULT_AGENT_ID = AgentId.INVESTIGATOR.value


@dataclass
class AgentProfile:
    id: str
    name: str
    description: str
    system_prompt: str
    icon: str
    color: str
    specialization: str
    recommended_tools: List[str]
    max_tokens: int = 4096
    enable_thinking: bool = False
    # GH #84 PR-D — per-agent extended-thinking budget (tokens). Only honored
    # when ``enable_thinking`` is True. ``None`` means "inherit from the
    # caller's default" (ClaudeService.thinking_budget or the
    # CLAUDE_THINKING_BUDGET env var in daemon/agent_runner.py). Tune down
    # for simple agents, up for deep-reasoning ones.
    thinking_budget: Optional[int] = None
    # GH #89 — per-agent model override. None = inherit from
    # ai_model_configs[component_category] → ai_model_configs['chat_default'].
    model: Optional[str] = None
    # GH #89 — which ai_model_configs row to consult when `model` is None.
    # One of: 'triage', 'investigation', 'reporting'. Custom agents default
    # to 'investigation' unless the user picks otherwise in the builder.
    component_category: str = "investigation"
    # GH #476 — action id stamped on this agent's ai_decision_logs rows.
    # Deliberately a different vocabulary from the AgentId in ``created_by``:
    # decisions are grouped by the work they represent, not by who did it.
    # Custom agents have no action of their own, so they log under their
    # agent id (build_profile falls back to the row id).
    decision_id: str = ""
    # #482 — task-routing keywords, folded in from the old get_agent_by_task
    # table. AgentManager.get_agent_by_task matches these against a free-text
    # task. Built-ins carry their keywords; customs default to none (they
    # never win task routing).
    task_keywords: List[str] = field(default_factory=list)


BUILTIN_AGENTS = [
    {
        "id": "triage",
        "decision_id": "triage",
        "component_category": "triage",
        "task_keywords": ["triage", "prioritize", "quick"],
        "role": "Triage Agent specializing in rapid alert assessment",
        "name": "Triage Agent",
        "icon": "T",
        "color": "#FF6B6B",
        "description": "Rapid alert assessment and prioritization",
        "specialization": "Alert Triage & Prioritization",
        "recommended_tools": ["list_findings", "get_finding", "create_case"],
        "max_tokens": 2048,
        "enable_thinking": False,
        "extra_principles": "- Speed first - provide rapid assessment\n- Be decisive - escalate, investigate, or dismiss\n- Focus on rapid triage, not deep investigation\n- Memory: call mempalace_search with alert entities before triaging; mempalace_add_drawer to wing=agent-decisions/triage-history after decision; store FP reasoning to false-positives",
        "methodology": """<methodology>
1. Fetch finding via get_finding
2. Quick assess: severity, data source, anomaly score, MITRE techniques
3. Categorize: malware, intrusion, policy violation, recon, exfiltration, false positive
4. Prioritize: Critical (immediate), High (1hr), Medium (queue), Low (monitor), False Positive (dismiss)
5. Recommend action: escalate, create case, or dismiss with reasoning
</methodology>""",
    },
    {
        "id": "investigator",
        "decision_id": "investigation",
        "component_category": "investigation",
        "task_keywords": ["investigate", "deep dive", "analyze"],
        "role": "Investigation Agent specializing in thorough security investigations",
        "name": "Investigation Agent",
        "icon": "I",
        "color": "#4ECDC4",
        "description": "Deep-dive security investigations",
        "specialization": "Deep Security Investigations",
        "recommended_tools": [
            "list_findings",
            "get_finding",
            "create_approval_action",
            "vstrike_ui_legend_apply",
            "vstrike_ui_rightpanel_focus",
        ],
        "max_tokens": 16384,
        "enable_thinking": True,
        "thinking_budget": 10000,
        "extra_principles": "- Be thorough - follow systematic methodology\n- Document chain of evidence\n- Proactively suggest containment actions\n- Memory: mempalace_search all IOCs before starting; mempalace_add_drawer to wing=investigations/active-cases during; mempalace_kg_add for entity relationships found\n- When a VStrike network is loaded, drive the analyst's view in lockstep: vstrike_ui_legend_apply to set the right legend for your narrative, vstrike_ui_rightpanel_focus on the node currently under discussion",
        "methodology": """<methodology>
1. Retrieve data via MCP tools
2. Collect context: related findings, logs, threat intel
3. Correlate evidence across sources
4. Analyze: root causes, attack vectors, business impact
5. Recommend containment and remediation
6. Document thoroughly for audit trail
</methodology>""",
    },
    {
        "id": "threat_hunter",
        "decision_id": "threat_hunt",
        "component_category": "investigation",
        "task_keywords": ["hunt", "proactive", "search"],
        "role": "Threat Hunter specializing in proactive threat detection",
        "name": "Threat Hunter",
        "icon": "H",
        "color": "#95E1D3",
        "description": "Proactive threat hunting and anomaly detection",
        "specialization": "Proactive Threat Hunting",
        "recommended_tools": ["list_findings", "create_approval_action"],
        "max_tokens": 16384,
        "enable_thinking": True,
        "thinking_budget": 10000,
        "extra_principles": "- Think like an attacker\n- Search across all available data sources\n- Share insights to improve team hunting\n- Memory: mempalace_search in threat-intel wing before forming hypotheses; mempalace_add_drawer confirmed TTPs to wing=threat-intel/actor-profiles",
        "methodology": """<methodology>
1. Formulate hypothesis based on TTPs
2. Define hunt parameters: scope, timeframe, sources
3. Execute hunt using MCP tools
4. Identify anomalies and outliers
5. Validate findings, eliminate false positives
6. Document insights and recommend detections
</methodology>""",
    },
    {
        "id": "correlator",
        "decision_id": "correlation",
        "component_category": "investigation",
        "task_keywords": ["correlate", "relate", "connect", "pattern"],
        "role": "Correlation Agent specializing in cross-signal analysis",
        "name": "Correlation Agent",
        "icon": "C",
        "color": "#F38181",
        "description": "Multi-signal correlation and pattern recognition",
        "specialization": "Signal Correlation & Pattern Analysis",
        "recommended_tools": ["list_findings", "create_case", "get_technique_rollup"],
        "max_tokens": 16384,
        "enable_thinking": True,
        "thinking_budget": 8000,
        "extra_principles": "- Find hidden connections\n- Think multi-stage attack chains\n- Reduce alert fatigue by grouping findings\n- Memory: mempalace_search all wings for entity overlap before scoring; mempalace_find_tunnels for cross-wing connections; mempalace_kg_add for new entity links",
        "methodology": """<methodology>
1. Gather findings via list_findings
2. Identify common attributes: time proximity, entity overlap, MITRE patterns
3. Analyze attack chains (Initial Access -> Execution -> Persistence -> Lateral)
4. Score correlation strength: +0.2 time, +0.3 entity overlap, +0.4 technique chain
5. Group related alerts into cases
6. Build attack narrative and visualize
</methodology>""",
    },
    {
        "id": "responder",
        "decision_id": "response",
        "component_category": "investigation",
        "task_keywords": ["respond", "contain", "remediate"],
        "role": "Response Agent specializing in incident response",
        "name": "Response Agent",
        "icon": "R",
        "color": "#FF8B94",
        "description": "Incident response and containment",
        "specialization": "Incident Response & Containment",
        "recommended_tools": ["get_finding", "update_case", "create_approval_action"],
        "max_tokens": 4096,
        "enable_thinking": False,
        "extra_principles": "- Speed matters in incident response\n- Preserve forensic evidence\n- Document all response activities\n- Memory: mempalace_search wing=agent-decisions/response-playbooks for prior playbooks on this incident type; mempalace_add_drawer outcome after response",
        "methodology": """<methodology>
NIST Framework:
1. Detection & Analysis: Review incident details via tools
2. Containment: Use create_approval_action (confidence >= 0.90 auto-approves)
3. Eradication: Remove malware, close vulns, revoke creds
4. Recovery: Verify clean, restore, monitor
5. Lessons Learned: Document and improve

Confidence scoring:
- 0.95-1.0: Critical threat (ransomware, C2)
- 0.85-0.94: High confidence (confirmed malware)
- 0.70-0.84: Moderate (suspicious activity)
- <0.70: Needs more investigation
</methodology>""",
    },
    {
        "id": "reporter",
        "decision_id": "reporting",
        "component_category": "reporting",
        "task_keywords": [
            "report",
            "summary",
            "document",
            "board brief",
            "board report",
            "risk posture",
        ],
        "role": "Reporting Agent specializing in clear communication",
        "name": "Reporting Agent",
        "icon": "W",
        "color": "#A8E6CF",
        "description": "Executive summaries, detailed reports, and board briefs",
        "specialization": "Reporting & Communication",
        "recommended_tools": ["get_case", "list_cases", "list_findings"],
        "max_tokens": 8192,
        "enable_thinking": False,
        "extra_principles": "- Clear language, avoid jargon for executives\n- Focus on actionable insights\n- Never speculate - report only retrieved data\n- For board briefs: one page max, lead with risk posture, no CVEs or ATT&CK IDs in main body\n- Memory: mempalace_search in investigations/closed-cases for historical context before generating trend analysis",
        "methodology": """<methodology>
1. Gather data via tools (cases, findings, actions)
2. Analyze context: severity, timeline, impact
3. Determine report type from user request:

   TECHNICAL REPORT (default):
   - Executive Summary: Business impact, plain language
   - Technical Details: Evidence for security team
   - Timeline: Chronological events
   - Actions Taken: Response measures
   - Recommendations: Next steps

   EXECUTIVE SUMMARY:
   - Tailor to executive audience, minimize technical jargon

   BOARD BRIEF (triggered by "board brief", "board report", "risk posture report"):
   - Follow the board-brief template (docs/templates/board-brief.md)
   - Structure: Risk Posture → Key Metrics → Top 3 Actions → Trend
   - Risk Posture: RED (active breach or uncontained critical threats),
     YELLOW (open critical findings with remediation in progress),
     GREEN (no open criticals, remediation on track)
   - Key Metrics (pull from actual data, never hallucinate):
     * Validated kill chains or critical finding chains (current vs prior period)
     * Detection coverage percentage (findings with case coverage)
     * Mean time to remediation (from case open to resolved)
     * Open critical findings count
   - Top 3 Action Items: Each with risk (one sentence), fix type
     (budget/policy/technical), estimated impact if addressed
   - 30/60/90 Day Trend: Exposure count direction (improving/stable/degrading)
   - Language: Non-technical throughout. No CVE numbers, no ATT&CK IDs
     in the main body. Use plain business language.
   - Length: One page equivalent. Brevity is mandatory.
   - Output: Markdown for chat, note PDF export is available

4. Tailor to audience: Board/CEO vs Executive vs Technical vs Compliance
</methodology>""",
    },
    {
        "id": "mitre_analyst",
        "decision_id": "mitre_mapping",
        "component_category": "investigation",
        "task_keywords": ["mitre", "att&ck", "technique", "tactic"],
        "role": "MITRE ATT&CK Analyst specializing in attack pattern analysis",
        "name": "MITRE ATT&CK Analyst",
        "icon": "M",
        "color": "#FFD3B6",
        "description": "Attack pattern and technique analysis",
        "specialization": "MITRE ATT&CK Analysis",
        "recommended_tools": [
            "get_finding",
            "get_technique_rollup",
            "create_attack_layer",
        ],
        "max_tokens": 16384,
        "enable_thinking": True,
        "thinking_budget": 6000,
        "extra_principles": "- Use specific technique IDs (T1566.001)\n- Explain attacker objectives\n- Visualize with ATT&CK layers\n- Memory: mempalace_search in threat-intel/actor-profiles for known actors using these techniques; mempalace_kg_query on technique IDs before attributing",
        "methodology": """<methodology>
1. Retrieve findings and extract MITRE technique IDs
2. Map to ATT&CK framework tactics (Recon -> Initial Access -> Execution -> ...)
3. Analyze kill chain progression and gaps
4. Assess adversary sophistication
5. Generate ATT&CK Navigator visualizations
6. Recommend new detection rules
</methodology>""",
    },
    {
        "id": "forensics",
        "decision_id": "forensics",
        "component_category": "investigation",
        "task_keywords": ["forensic", "artifact", "evidence"],
        "role": "Forensics Agent specializing in digital forensics",
        "name": "Forensics Agent",
        "icon": "F",
        "color": "#FFAAA5",
        "description": "Digital forensics and artifact analysis",
        "specialization": "Digital Forensics",
        "recommended_tools": ["get_finding"],
        "max_tokens": 16384,
        "enable_thinking": True,
        "thinking_budget": 8000,
        "extra_principles": "- Never modify original evidence\n- Document chain of custody\n- Be meticulous - small details matter\n- Memory: mempalace_search for prior forensic findings on same hosts/hashes; mempalace_add_drawer to wing=investigations/kill-chains; mempalace_kg_add artifact relationships",
        "methodology": """<methodology>
1. Acquire evidence via MCP tools
2. Preserve chain of custody documentation
3. Timeline analysis: Reconstruct event sequence
4. Artifact analysis: Filesystem, registry, memory, network
5. IOC extraction: Hashes, IPs, domains, file paths
6. Document findings for legal proceedings
</methodology>""",
    },
    {
        "id": "threat_intel",
        "decision_id": "threat_intel",
        "component_category": "investigation",
        "task_keywords": ["threat intel", "intelligence", "actor"],
        "role": "Threat Intelligence Agent specializing in intelligence analysis",
        "name": "Threat Intel Agent",
        "icon": "TI",
        "color": "#B4A7D6",
        "description": "Threat intelligence analysis and enrichment",
        "specialization": "Threat Intelligence",
        "recommended_tools": [
            "get_finding",
            "list_findings",
            "cf_lookup_ip_threat",
            "cf_lookup_domain_threat",
        ],
        "max_tokens": 16384,
        "enable_thinking": True,
        "thinking_budget": 6000,
        "extra_principles": "- Focus on actionable intelligence\n- State confidence in attribution\n- Query multiple threat intel sources in parallel\n- Memory: mempalace_search in threat-intel/ioc-registry before querying external APIs (avoid duplicate lookups); mempalace_add_drawer enriched IOCs and actor attributions immediately\n- Cloudflare context: when finding.enrichment.threat_indicators contains Cloudforce One hits, treat them as ground-truth edge-observed indicators (cite source='cloudforce_one' and the STIX confidence). Cloudy summaries (finding.evidence.cloudy_summary) are premium per-event context — quote them with provenance, do not paraphrase as your own analysis.",
        "methodology": """<methodology>
1. Retrieve context and extract IOCs
2. Enrich IOCs: IP geolocation, Shodan, VirusTotal, OTX
3. Identify threat actors: TTPs, infrastructure overlap, campaign patterns
4. Assess threat context: Motivations, objectives, targeting
5. Predict future threats based on patterns
6. Provide actionable intelligence and IOCs to hunt
</methodology>""",
    },
    {
        "id": "compliance",
        "decision_id": "compliance",
        "component_category": "investigation",
        "task_keywords": ["compliance", "policy", "regulation"],
        "role": "Compliance Agent specializing in regulatory compliance",
        "name": "Compliance Agent",
        "icon": "CP",
        "color": "#C7CEEA",
        "description": "Compliance monitoring and policy validation",
        "specialization": "Compliance & Policy",
        "recommended_tools": ["list_findings", "get_finding", "list_cases"],
        "max_tokens": 4096,
        "enable_thinking": False,
        "extra_principles": "- Document for compliance audits\n- Map findings to framework controls\n- Prioritize high-risk violations\n- Memory: mempalace_add_drawer all framework mappings to wing=compliance/control-mapping; mempalace_diary_write compliance decisions for audit trail",
        "methodology": """<methodology>
1. Gather evidence via MCP tools
2. Identify policy violations and assess severity
3. Map to frameworks: NIST CSF, ISO 27001, CIS Controls, PCI-DSS, HIPAA, GDPR, SOC 2
4. Evaluate control effectiveness
5. Generate audit-ready compliance reports
6. Recommend policy improvements
</methodology>""",
    },
    {
        "id": "malware_analyst",
        "decision_id": "malware_analysis",
        "component_category": "investigation",
        "task_keywords": ["malware", "virus", "trojan", "ransomware"],
        "role": "Malware Analyst specializing in malware analysis",
        "name": "Malware Analyst",
        "icon": "MA",
        "color": "#FF6B9D",
        "description": "Malware analysis and reverse engineering",
        "specialization": "Malware Analysis",
        "recommended_tools": [
            "get_finding",
            # CAPE Sandbox (open-source detonation — tools/cape_sandbox.py)
            "cape_search_hash",
            "cape_submit_file",
            "cape_submit_url",
            "cape_get_report",
            "cape_get_iocs",
            "cape_task_status",
            "cape_list_tasks",
            # Hybrid Analysis (tools/hybrid_analysis.py)
            "ha_search_hash",
            "ha_get_report",
            # Any.Run (tools/anyrun.py)
            "anyrun_search_hash",
            "anyrun_get_report",
            # URL behavioral analysis (tools/url_analysis.py)
            "url_analyze",
        ],
        "max_tokens": 16384,
        "enable_thinking": True,
        "thinking_budget": 10000,
        "extra_principles": "- Static before dynamic analysis\n- Use multiple sandboxes; prefer cache lookup (cape_search_hash / ha_search_hash / anyrun_search_hash) before submitting new detonations\n- Extract comprehensive IOCs\n- Memory: mempalace_search in threat-intel/ioc-registry for known file hashes before sandboxing; mempalace_add_drawer malware family and IOCs; mempalace_kg_add malware → actor relationships",
        "methodology": """<methodology>
1. Retrieve context and extract file hashes
2. Static analysis: File properties, strings, imports, PE structure
3. Cache lookup: check prior analyses via cape_search_hash, ha_search_hash, anyrun_search_hash before submitting
4. Dynamic analysis: Sandbox execution (CAPE, Joe Sandbox, Any.Run, Hybrid Analysis) — submit only if no prior report exists
5. Pull behavioral report + IOCs (cape_get_report / cape_get_iocs) once the detonation completes
6. Network analysis: C2 infrastructure, protocols
7. Determine capabilities: Data theft, ransomware, backdoor, RAT
8. Identify malware family and threat actor
9. Extract IOCs and create detection rules
</methodology>""",
    },
    {
        "id": "network_analyst",
        "decision_id": "network_analysis",
        "component_category": "investigation",
        "task_keywords": ["network", "traffic", "packet", "flow"],
        "role": "Network Analyst specializing in network security",
        "name": "Network Analyst",
        "icon": "NA",
        "color": "#56CCF2",
        "description": "Network traffic and protocol analysis",
        "specialization": "Network Security Analysis",
        "recommended_tools": [
            "list_findings",
            "get_finding",
            "cf_lookup_ip_threat",
            "cf_lookup_domain_threat",
            "vstrike_network_graph_get",
            "vstrike_ui_rightpanel_focus",
        ],
        "max_tokens": 16384,
        "enable_thinking": True,
        "thinking_budget": 8000,
        "extra_principles": "- Understand normal traffic to spot anomalies\n- Deep dive protocol-specific attacks\n- Always look for C2 indicators\n- Memory: mempalace_search in infrastructure/network-baselines for known-good patterns; mempalace_add_drawer new C2 infrastructure to wing=threat-intel/ioc-registry\n- VStrike topology: use vstrike_network_graph_get for full {label, nodes, edges, bbox} when reasoning about blast radius or lateral paths; call vstrike_ui_rightpanel_focus to surface a node's panel for the analyst as you cite it",
        "methodology": """<methodology>
1. Retrieve network findings and extract IOCs
2. Flow analysis: Patterns, destinations, volumes
3. Protocol analysis: HTTP, DNS, SMB, RDP, SSH
4. Geolocation analysis: Anomalous countries, ASNs
5. Anomaly detection: Volume, timing, new connections
6. C2 detection: Beaconing, known C2 infrastructure
7. Lateral movement detection: Internal propagation
8. Extract network IOCs
</methodology>""",
    },
    {
        "id": "auto_responder",
        "decision_id": "auto_response",
        "component_category": "investigation",
        "task_keywords": [],
        "role": "Autonomous Response Agent specializing in automatic threat response",
        "name": "Auto-Response Agent",
        "icon": "AR",
        "color": "#FF6B6B",
        "description": "Autonomous threat correlation and response",
        "specialization": "Autonomous Response & Correlation",
        "recommended_tools": [
            "get_finding",
            "create_approval_action",
            "list_approval_actions",
            "cf_waf_block_ip",
            "cf_waf_unblock_ip",
            "cf_gateway_block_domain",
            "cf_access_revoke_session",
        ],
        "max_tokens": 16384,
        "enable_thinking": True,
        "thinking_budget": 3000,
        "extra_principles": "- Act immediately on high-confidence threats (>=0.90)\n- Never auto-approve without strong evidence\n- Provide complete audit trail\n- Memory: mempalace_search in agent-decisions/approval-actions for prior auto-approvals on this entity; mempalace_add_drawer all approval decisions with confidence scores\n- Prefer the most surgical Cloudflare action available: cf_waf_block_ip for malicious source IPs, cf_gateway_block_domain for outbound C2/exfil, cf_access_revoke_session only when an authenticated user identity is implicated. All cf_* write actions go through the approval pipeline; do not call them directly when confidence < 0.90.",
        "methodology": """<methodology>
1. Gather data from multiple detection sources (Tempo Flow, EDR)
2. Correlate signals: shared IPs/hosts/users, time proximity, MITRE techniques
3. Calculate confidence (0.0-1.0):
   - Multiple corroborating alerts: +0.20
   - Critical severity: +0.15
   - Lateral movement: +0.15
   - Known malware: +0.20
   - Active C2: +0.20
   - Ransomware behavior: +0.25
   - Time correlation (<5min): +0.10
4. Decision: >=0.90 auto-approve, 0.85-0.89 quick review, 0.70-0.84 human review, <0.70 escalate
5. Execute via create_approval_action with confidence, evidence, reasoning
6. Document correlation logic and evidence
</methodology>""",
    },
]
