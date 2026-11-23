"""The one Report Check registry.

Each check has Vigil's own id, its module, the permission that gates its
source, and its rule. A value check also carries the cited value, the source
id, the clause, the link and the date it was checked. There is no field for
quoted text. Outcomes are not stored here; they live on the Report Record.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class ReportModule:
    id: str
    permission: str


@dataclass(frozen=True)
class ReportCheck:
    id: str
    module: str
    permission: str
    rule: str
    cited_value: Optional[str] = None
    source_id: Optional[str] = None
    clause: Optional[str] = None
    link: Optional[str] = None
    date_checked: Optional[str] = None


VISIBILITY = ReportModule("visibility", "integrations.read")
# The page already requires platform.read. The section is not hidden as a
# whole: a Manager holds audit.read and not sessions.manage, and still sees
# retention. Each check's own permission is applied when the record is read.
SETTINGS = ReportModule("settings", "platform.read")
COVERAGE = ReportModule("coverage", "detections.read")
# Hosts CrowdStrike, Defender and Wazuh still list. Counts only.
ESTATE = ReportModule("estate", "integrations.read")
# Report Sections: figures and lists, no checks. Stored under evidence.
HISTORY_BEFORE = ReportModule("history.before", "findings.read")
HISTORY_RECHECK = ReportModule("history.recheck", "findings.read")
HISTORY_TUNING = ReportModule("history.tuning", "findings.read")
HISTORY = (HISTORY_BEFORE, HISTORY_RECHECK, HISTORY_TUNING)

_CHECKED = "2026-11-23"
_NIST = "https://csrc.nist.gov/pubs/sp/800/63/b/4/final"
_CJIS = "https://le.fbi.gov/file-repository/cjis_security_policy_v6-0_20241227.pdf"
_FEDRAMP = (
    "https://fedramp.gov/2026/reference/controls/identification-and-authentication/"
)
_NYCRR = "https://www.dfs.ny.gov/cybersecurity/23-NYCRR-Part-500"
_CFR = "https://www.ecfr.gov/current/title-16/chapter-I/subchapter-C/part-314"
_SESSIONS = "sessions.manage"

# Ids are not prefixes of each other: spec_for also matches "id.".
CHECKS: tuple[ReportCheck, ...] = (
    ReportCheck(
        "visibility.daemon",
        VISIBILITY.id,
        VISIBILITY.permission,
        "The background service is running.",
    ),
    ReportCheck(
        "visibility.model",
        VISIBILITY.id,
        VISIBILITY.permission,
        "A model is assigned and its provider is routable.",
    ),
    ReportCheck(
        "visibility.collection",
        VISIBILITY.id,
        VISIBILITY.permission,
        "At least one alert source is switched on, and every alert source is collected by scheduled collection or an enabled receiver.",
    ),
    ReportCheck(
        "visibility.source",
        VISIBILITY.id,
        VISIBILITY.permission,
        "A switched-on source is healthy. Last data and webhook labels are information.",
    ),
    ReportCheck(
        "settings.idle",
        SETTINGS.id,
        _SESSIONS,
        "A standard sign-in ends after the idle limit with no activity.",
        "15 minutes or fewer",
        "NIST SP 800-63B-4",
        "AAL3 reauthentication",
        _NIST,
        _CHECKED,
    ),
    ReportCheck(
        "settings.length",
        SETTINGS.id,
        _SESSIONS,
        "A sign-in ends after the overall limit whether or not it is in use.",
        "12 hours or fewer",
        "NIST SP 800-63B-4; CJIS Security Policy v6.0",
        "AAL3 reauthentication; IA-11",
        f"{_NIST}; {_CJIS}",
        _CHECKED,
    ),
    ReportCheck(
        "settings.lockout",
        SETTINGS.id,
        _SESSIONS,
        "Failed sign-ins lock the account until an administrator releases it.",
        "3 failures in a row, locked until an administrator releases it. Vigil counts failures with no time window, which is at least as strict as a 15-minute window.",
        "DISA Application Security and Development STIG (2025-09-09); CJIS Security Policy v6.0",
        "V-222432; AC-7",
        "https://www.stigviewer.com/stigs/application_security_and_development/2025-09-09/finding/V-222432; "
        + _CJIS,
        _CHECKED,
    ),
    ReportCheck(
        "settings.dormancy",
        SETTINGS.id,
        _SESSIONS,
        "An account with no sign-in and no personal token use is deactivated.",
        "45 days or fewer",
        "CIS Controls v8",
        "Safeguard 5.3",
        "https://controls-assessment-specification.readthedocs.io/en/latest/control-5/control-5.3.html",
        _CHECKED,
    ),
    ReportCheck(
        "settings.unused",
        SETTINGS.id,
        _SESSIONS,
        "A token left unused is revoked.",
        "45 days unused or fewer. No cited source sets a period for API tokens; this is the nearest one.",
        "AWS Security Hub",
        "IAM.22",
        "https://docs.aws.amazon.com/securityhub/latest/userguide/iam-controls.html#iam-22",
        _CHECKED,
    ),
    ReportCheck(
        "settings.mfa_privileged",
        SETTINGS.id,
        _SESSIONS,
        "Privileged accounts must use MFA.",
        "required for privileged accounts",
        "NIST SP 800-53 Rev. 5, in FedRAMP Consolidated Rules for 2026",
        "IA-2(1)",
        _FEDRAMP,
        _CHECKED,
    ),
    ReportCheck(
        "settings.phishing_resistant",
        SETTINGS.id,
        _SESSIONS,
        "Privileged accounts must use a security key.",
        "required for privileged accounts",
        "FedRAMP Consolidated Rules for 2026",
        "IA-2(1) guidance",
        _FEDRAMP,
        _CHECKED,
    ),
    ReportCheck(
        "settings.mfa_everyone",
        SETTINGS.id,
        _SESSIONS,
        "Every person must use MFA.",
        "required for every person",
        "23 NYCRR 500; 16 CFR 314",
        "500.12; 314.4(c)(5)",
        f"{_NYCRR}; {_CFR}",
        _CHECKED,
    ),
    ReportCheck(
        "settings.retention",
        SETTINGS.id,
        "audit.read",
        "The audit trail is kept for a period that is in force.",
        "at least 3 years (1,095 days), for trails of cybersecurity events. A longer period in the same clause covers financial transactions, which this trail does not hold",
        "23 NYCRR 500",
        "500.6(b)",
        _NYCRR,
        _CHECKED,
    ),
    ReportCheck(
        "coverage.technique",
        COVERAGE.id,
        COVERAGE.permission,
        "A technique is proven only when an active SIEM copy passes Runs and Sees.",
    ),
    ReportCheck(
        "coverage.blind_spot",
        COVERAGE.id,
        COVERAGE.permission,
        "A data source SIEM rules fail Sees on is a blind spot.",
    ),
    ReportCheck(
        "coverage.paper",
        COVERAGE.id,
        COVERAGE.permission,
        "A technique is covered on paper only when every deployed SIEM rule on it fails Runs or Sees.",
    ),
    ReportCheck(
        "coverage.run",
        COVERAGE.id,
        COVERAGE.permission,
        "A rule fails to run, or its data is silent.",
    ),
    ReportCheck(
        "coverage.quiet",
        COVERAGE.id,
        COVERAGE.permission,
        "Quiet fails a rule over its alerts-per-day limit.",
    ),
    ReportCheck(
        "coverage.noisy",
        COVERAGE.id,
        COVERAGE.permission,
        "Noisy fails a rule with far more alerts today than its usual.",
    ),
    ReportCheck(
        "coverage.silent",
        COVERAGE.id,
        COVERAGE.permission,
        "Silent fails a rule that fired regularly, then stopped.",
    ),
    ReportCheck(
        "estate.setup",
        ESTATE.id,
        ESTATE.permission,
        "CrowdStrike, Defender or Wazuh is set up. With none of them, the read is not checked.",
    ),
    ReportCheck(
        "estate.crowdstrike.unheard",
        ESTATE.id,
        ESTATE.permission,
        "CrowdStrike hosts last seen 7 days or more before the read. Needs a person when any, pass when none.",
    ),
    ReportCheck(
        "estate.crowdstrike.degraded",
        ESTATE.id,
        ESTATE.permission,
        "CrowdStrike hosts in reduced functionality mode. Fail when any, pass when none. A blank value is not counted.",
    ),
    ReportCheck(
        "estate.defender.unheard",
        ESTATE.id,
        ESTATE.permission,
        "Onboarded Defender machines whose last full report is 7 days or more before the read. Needs a person when any, pass when none.",
    ),
    ReportCheck(
        "estate.defender.degraded",
        ESTATE.id,
        ESTATE.permission,
        "Onboarded Defender machines reporting impaired communication or no sensor data. Fail when any, pass when none.",
    ),
    ReportCheck(
        "estate.wazuh.unheard",
        ESTATE.id,
        ESTATE.permission,
        "Wazuh agents last heard 7 days or more before the read, and never-connected agents once registration is 7 days old. The manager is not counted.",
    ),
)

MODULES = {
    VISIBILITY.id: VISIBILITY,
    SETTINGS.id: SETTINGS,
    COVERAGE.id: COVERAGE,
    ESTATE.id: ESTATE,
    HISTORY_BEFORE.id: HISTORY_BEFORE,
    HISTORY_RECHECK.id: HISTORY_RECHECK,
    HISTORY_TUNING.id: HISTORY_TUNING,
}


def spec_for(check_id: str) -> ReportCheck:
    """The registry entry a produced check id belongs to."""
    for spec in CHECKS:
        if check_id == spec.id or check_id.startswith(spec.id + "."):
            return spec
    raise KeyError(check_id)
