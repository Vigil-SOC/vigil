"""The one Report Check registry.

Each check has Vigil's own id, its module, the permission that gates its
source, and its rule. A settings check carries its scoped values beside that
rule: jurisdictions and kinds taken from the selection catalogue, or every
install, and a ceiling, floor or level. There is no field for quoted text and
no separate wording field. Outcomes are not stored here; they live on the
Report Record.

``LEGACY_CITATIONS`` is the citation text this module showed before values
were scoped. A record that stored no citation keeps that text. The live
registry is not a substitute for it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Optional

from core.reporting.selection import JURISDICTIONS, KINDS

# "statement": met by Provider Statements; ``bound`` names the rule
# (core/reporting/day1.py, _MODEL_RULES). Every held one applies, not the tightest.
Direction = Literal["ceiling", "floor", "level", "statement"]
ValueStatus = Literal["in_force", "proposed"]

_LEVEL_RANK = {"off": 0, "nobody": 0, "privileged": 1, "everyone": 2}

_NIST = "https://csrc.nist.gov/pubs/sp/800/63/b/4/final"
_CIS_6 = "https://cas.docs.cisecurity.org/en/latest/source/Controls6/"
_CISA = "https://www.cisa.gov/cybersecurity-performance-goals-2-0-cpg-2-0"
_CIS_5 = (
    "https://controls-assessment-specification.readthedocs.io/en/latest/"
    "control-5/control-5.3.html"
)
_AWS = (
    "https://docs.aws.amazon.com/securityhub/latest/userguide/iam-controls.html#iam-22"
)
_PCI = (
    "https://www.pcisecuritystandards.org/document_library/"
    "?category=pcidss&document=pci_dss"
)
_CFR = "https://www.ecfr.gov/current/title-16/chapter-I/subchapter-C/part-314"
_NYCRR = "https://www.dfs.ny.gov/cybersecurity/23-NYCRR-Part-500"
_HIPAA = "https://www.reginfo.gov/public/do/eAgendaViewRule?pubId=202510&RIN=0945-AA22"
_STIG_LOCK = (
    "https://www.stigviewer.com/stigs/application_security_and_development/"
    "2025-09-09/finding/V-222432"
)
_STIG_DORMANT = (
    "https://www.stigviewer.com/stigs/application_security_and_development/"
    "2025-09-09/finding/V-222411"
)
_CJIS = (
    "https://www.dps.texas.gov/sites/default/files/documents/securityreview/"
    "documents/cjisSecurityPolicy_v6-1.pdf"
)
_FEDRAMP = (
    "https://fedramp.gov/2026/reference/controls/identification-and-authentication/"
)
_IRS = "https://www.irs.gov/pub/irs-pdf/p1075.pdf"
_BA = "https://www.ecfr.gov/current/title-45/section-164.308"
_GDPR = "https://eur-lex.europa.eu/legal-content/EN/TXT/HTML/?uri=CELEX:32016R0679"
_DPF = "https://eur-lex.europa.eu/legal-content/EN/TXT/HTML/?uri=CELEX:32023D1795"
_E8 = (
    "https://www.cyber.gov.au/business-government/asds-cyber-security-frameworks/"
    "essential-eight/essential-eight-maturity-model"
)
_SESSIONS = "sessions.manage"

# The citation block a record stored before scoped values. Frozen: a later
# edit to the live values must not change what those records show.
_LEGACY_CJIS = (
    "https://le.fbi.gov/file-repository/cjis_security_policy_v6-0_20241227.pdf"
)
_LEGACY_CHECKED = "2026-11-23"
LEGACY_CITATIONS: dict[str, dict[str, str]] = {
    "settings.idle": {
        "cited_value": "15 minutes or fewer",
        "source_id": "NIST SP 800-63B-4",
        "clause": "AAL3 reauthentication",
        "link": _NIST,
        "date_checked": _LEGACY_CHECKED,
    },
    "settings.length": {
        "cited_value": "12 hours or fewer",
        "source_id": "NIST SP 800-63B-4; CJIS Security Policy v6.0",
        "clause": "AAL3 reauthentication; IA-11",
        "link": f"{_NIST}; {_LEGACY_CJIS}",
        "date_checked": _LEGACY_CHECKED,
    },
    "settings.lockout": {
        "cited_value": (
            "3 failures in a row, locked until an administrator releases it. "
            "Vigil counts failures with no time window, which is at least as "
            "strict as a 15-minute window."
        ),
        "source_id": (
            "DISA Application Security and Development STIG (2025-09-09); "
            "CJIS Security Policy v6.0"
        ),
        "clause": "V-222432; AC-7",
        "link": (
            "https://www.stigviewer.com/stigs/application_security_and_development/"
            f"2025-09-09/finding/V-222432; {_LEGACY_CJIS}"
        ),
        "date_checked": _LEGACY_CHECKED,
    },
    "settings.dormancy": {
        "cited_value": "45 days or fewer",
        "source_id": "CIS Controls v8",
        "clause": "Safeguard 5.3",
        "link": _CIS_5,
        "date_checked": _LEGACY_CHECKED,
    },
    "settings.unused": {
        "cited_value": (
            "45 days unused or fewer. No cited source sets a period for API "
            "tokens; this is the nearest one"
        ),
        "source_id": "AWS Security Hub",
        "clause": "IAM.22",
        "link": _AWS,
        "date_checked": _LEGACY_CHECKED,
    },
    "settings.mfa_privileged": {
        "cited_value": "required for privileged accounts",
        "source_id": "NIST SP 800-53 Rev. 5, in FedRAMP Consolidated Rules for 2026",
        "clause": "IA-2(1)",
        "link": _FEDRAMP,
        "date_checked": _LEGACY_CHECKED,
    },
    "settings.phishing_resistant": {
        "cited_value": "required for privileged accounts",
        "source_id": "FedRAMP Consolidated Rules for 2026",
        "clause": "IA-2(1) guidance",
        "link": _FEDRAMP,
        "date_checked": _LEGACY_CHECKED,
    },
    "settings.mfa_everyone": {
        "cited_value": "required for every person",
        "source_id": "23 NYCRR 500; 16 CFR 314",
        "clause": "500.12; 314.4(c)(5)",
        "link": f"{_NYCRR}; {_CFR}",
        "date_checked": _LEGACY_CHECKED,
    },
    "settings.retention": {
        "cited_value": (
            "at least 3 years (1,095 days), for trails of cybersecurity events. "
            "A longer period in the same clause covers financial transactions, "
            "which this trail does not hold"
        ),
        "source_id": "23 NYCRR 500",
        "clause": "500.6(b)",
        "link": _NYCRR,
        "date_checked": _LEGACY_CHECKED,
    },
}


@dataclass(frozen=True)
class ReportModule:
    id: str
    permission: str


@dataclass(frozen=True)
class ScopedValue:
    """One cited value. Empty jurisdictions and kinds means every install.

    A part the source does not state stays unset. ``window_minutes`` is shown
    and not scored. ``release_gap`` is a stated gap, not a failure.
    """

    jurisdictions: tuple[str, ...]
    kinds: tuple[str, ...]
    direction: Direction
    bound: int | str
    cited_value: str
    source_id: str
    clause: str
    link: str
    date_checked: str
    status: ValueStatus = "in_force"
    above: Optional[str] = None
    window_minutes: Optional[int] = None
    release_gap: Optional[str] = None
    written_alternative: bool = False


@dataclass(frozen=True)
class Resolved:
    """The in-force value to score, plus parts that are shown and not scored."""

    scored: Optional[ScopedValue]
    proposed: tuple[ScopedValue, ...]
    window: Optional[ScopedValue]
    gap: Optional[ScopedValue]


@dataclass(frozen=True)
class ReportCheck:
    id: str
    module: str
    permission: str
    rule: str
    values: tuple[ScopedValue, ...] = ()


def _scope(kind_id: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """The kind and its own jurisdiction. PCI names no jurisdiction."""
    kind = next(row for row in KINDS if row.id == kind_id)
    if kind.jurisdiction is not None and kind.jurisdiction not in {
        row.id for row in JURISDICTIONS
    }:
        raise RuntimeError(kind.jurisdiction)
    places = (kind.jurisdiction,) if kind.jurisdiction else ()
    return places, (kind.id,)


def _cited(
    kind_id: Optional[str],
    *,
    direction: Direction,
    bound: int | str,
    cited_value: str,
    source_id: str,
    clause: str,
    link: str,
    date_checked: str,
    status: ValueStatus = "in_force",
    above: Optional[str] = None,
    window_minutes: Optional[int] = None,
    release_gap: Optional[str] = None,
    written_alternative: bool = False,
) -> ScopedValue:
    if kind_id is None:
        places: tuple[str, ...] = ()
        kinds: tuple[str, ...] = ()
    else:
        places, kinds = _scope(kind_id)
    return ScopedValue(
        places,
        kinds,
        direction,
        bound,
        cited_value,
        source_id,
        clause,
        link,
        date_checked,
        status,
        above,
        window_minutes,
        release_gap,
        written_alternative,
    )


_EVERY = None
_PRIVILEGED = "required for privileged accounts"
_EVERYONE = "required for every person"

VISIBILITY = ReportModule("visibility", "integrations.read")
# Figures and lists, no outcomes. The page already requires platform.read.
FORECAST = ReportModule("history.forecast", "platform.read")
# The page already requires platform.read. The section is not hidden as a
# whole: a Manager holds audit.read and not sessions.manage, and still sees
# retention. Each check's own permission is applied when the record is read.
SETTINGS = ReportModule("settings", "platform.read")
COVERAGE = ReportModule("coverage", "detections.read")
# Hosts CrowdStrike, Defender and Wazuh still list. Counts only.
ESTATE = ReportModule("estate", "integrations.read")
# The declared site against what a sensor lists. Counts and item states only.
SITE = ReportModule("site", "platform.read")
# Report Sections: figures and lists, no checks. Stored under evidence.
HISTORY_BEFORE = ReportModule("history.before", "findings.read")
HISTORY_RECHECK = ReportModule("history.recheck", "findings.read")
# Figures only. The body is evidence["history.capped_recheck"]. Same id as
# capped_recheck.MODULE_ID, which the driver already treats as its switch.
HISTORY_CAPPED = ReportModule("history.capped_recheck", "findings.read")
HISTORY_TUNING = ReportModule("history.tuning", "findings.read")
HISTORY = (HISTORY_BEFORE, HISTORY_RECHECK, HISTORY_TUNING)
# The Production Start Report. Day 1's build does not call these. Confidence
# and agreement are figures only: they have no checks.
RUNNING = ReportModule("production.running", "platform.read")
QUIET = ReportModule("production.quiet", "findings.read")
# Figures, no checks. The body is evidence["production.alerts"].
ALERTS = ReportModule("production.alerts", "findings.read")
# Figures, no checks. The body is evidence["production.tuning"].
TUNING = ReportModule("production.tuning", "findings.read")
CONFIDENCE = ReportModule("production.confidence", "platform.read")
AGREEMENT = ReportModule("production.agreement", "platform.read")
# Figures, no checks. The body is evidence["production.spend"].
SPEND = ReportModule("production.spend", "platform.read")
# Information only: no checks. Day 1's build does not call it. The page
# already requires platform.read, and each item keeps its own permission.
CHANGES = ReportModule("production.changes", "platform.read")
# Figures only, no checks. Day 1's build does not call it.
TIME = ReportModule("production.time", "findings.read")

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
        "A sign-in ends after the looser of the standard and privileged idle limits with no activity. 0 is no idle limit.",
        (
            _cited(
                _EVERY,
                direction="ceiling",
                bound=15,
                cited_value="15 minutes or fewer",
                source_id="NIST SP 800-63B-4",
                clause="AAL3 reauthentication",
                link=_NIST,
                date_checked="2026-11-10",
            ),
        ),
    ),
    ReportCheck(
        "settings.length",
        SETTINGS.id,
        _SESSIONS,
        "A sign-in ends after the overall limit whether or not it is in use.",
        (
            _cited(
                _EVERY,
                direction="ceiling",
                bound=12,
                cited_value="12 hours or fewer",
                source_id="NIST SP 800-63B-4",
                clause="AAL3 reauthentication",
                link=_NIST,
                date_checked="2026-11-10",
            ),
        ),
    ),
    ReportCheck(
        "settings.lockout",
        SETTINGS.id,
        _SESSIONS,
        "Failed sign-ins lock the account until an administrator releases it.",
        (
            _cited(
                "dod_it",
                direction="ceiling",
                bound=3,
                cited_value="3 failures or fewer",
                source_id="DISA Application Security and Development STIG",
                clause="V-222432, required by DoDI 8500.01 Encl. 2 §13.u",
                link=_STIG_LOCK,
                date_checked="2026-11-19",
                window_minutes=15,
            ),
            _cited(
                "criminal_justice_information",
                direction="ceiling",
                bound=5,
                cited_value="5 failures or fewer",
                source_id="CJIS Security Policy v6.1",
                clause="AC-7",
                link=_CJIS,
                date_checked="2026-11-19",
                release_gap=(
                    "Held until an administrator releases it. Vigil's lock also "
                    "ends at the person's own reset link."
                ),
            ),
        ),
    ),
    ReportCheck(
        "settings.dormancy",
        SETTINGS.id,
        _SESSIONS,
        "An account with no sign-in and no personal token use is deactivated.",
        (
            _cited(
                _EVERY,
                direction="ceiling",
                bound=45,
                cited_value="45 days or fewer",
                source_id="CIS Controls v8",
                clause="Safeguard 5.3",
                link=_CIS_5,
                date_checked="2026-11-15",
            ),
            _cited(
                "pci_dss_assessment",
                direction="ceiling",
                bound=90,
                cited_value="90 days or fewer",
                source_id="PCI DSS v4.0.1",
                clause="8.2.6",
                link=_PCI,
                date_checked="2026-11-19",
            ),
            _cited(
                "dod_it",
                direction="ceiling",
                bound=35,
                cited_value="35 days or fewer",
                source_id="DISA Application Security and Development STIG",
                clause="V-222411, required by DoDI 8500.01 Encl. 2 §13.u",
                link=_STIG_DORMANT,
                date_checked="2026-11-19",
            ),
            _cited(
                "criminal_justice_information",
                direction="ceiling",
                bound=90,
                cited_value="90 days or fewer",
                source_id="CJIS Security Policy v6.1",
                clause="AC-2(3)(d)",
                link=_CJIS,
                date_checked="2026-11-19",
            ),
        ),
    ),
    ReportCheck(
        "settings.unused",
        SETTINGS.id,
        _SESSIONS,
        "A token left unused is revoked.",
        (
            _cited(
                _EVERY,
                direction="ceiling",
                bound=45,
                cited_value=(
                    "45 days unused or fewer. No cited source sets a period for "
                    "API tokens; this is the nearest one"
                ),
                source_id="AWS Security Hub",
                clause="IAM.22",
                link=_AWS,
                date_checked="2026-11-17",
            ),
        ),
    ),
    ReportCheck(
        "settings.mfa_privileged",
        SETTINGS.id,
        _SESSIONS,
        "Privileged accounts must use MFA.",
        (
            _cited(
                _EVERY,
                direction="level",
                bound="privileged",
                above="everyone",
                cited_value=_PRIVILEGED,
                source_id="CIS Controls v8.1",
                clause="Safeguard 6.5",
                link=_CIS_6,
                date_checked="2026-11-19",
            ),
            _cited(
                "dfs_limited_exemption",
                direction="level",
                bound="privileged",
                above="everyone",
                cited_value=_PRIVILEGED,
                source_id="23 NYCRR 500",
                clause="500.12(a)",
                link=_NYCRR,
                date_checked="2026-11-10",
                written_alternative=True,
            ),
            _cited(
                "non_corporate_commonwealth_entity",
                direction="level",
                bound="privileged",
                above="everyone",
                cited_value=_PRIVILEGED,
                source_id="Essential Eight (Nov 2023)",
                clause="Maturity Level Two, required by PSPF Release 2026 requirement 0101",
                link=_E8,
                date_checked="2026-11-22",
            ),
        ),
    ),
    ReportCheck(
        "settings.phishing_resistant",
        SETTINGS.id,
        _SESSIONS,
        "Privileged accounts must use a security key.",
        (
            _cited(
                "fedramp",
                direction="level",
                bound="privileged",
                above="everyone",
                cited_value=_PRIVILEGED,
                source_id="FedRAMP Consolidated Rules for 2026",
                clause="IA-2(1) guidance",
                link=_FEDRAMP,
                date_checked="2026-11-16",
            ),
        ),
    ),
    ReportCheck(
        "settings.phishing_resistant_everyone",
        SETTINGS.id,
        _SESSIONS,
        "Every person must use a security key.",
        (
            _cited(
                "non_corporate_commonwealth_entity",
                direction="level",
                bound="everyone",
                cited_value=_EVERYONE,
                source_id="Essential Eight (Nov 2023)",
                clause="Maturity Level Two, required by PSPF Release 2026 requirement 0101",
                link=_E8,
                date_checked="2026-11-22",
            ),
        ),
    ),
    ReportCheck(
        "settings.mfa_everyone",
        SETTINGS.id,
        _SESSIONS,
        "Every person must use MFA.",
        (
            _cited(
                _EVERY,
                direction="level",
                bound="everyone",
                cited_value=_EVERYONE,
                source_id="CISA Cybersecurity Performance Goals 2.0",
                clause="goal 3.F",
                link=_CISA,
                date_checked="2026-11-19",
            ),
            _cited(
                "cardholder_data_environment",
                direction="level",
                bound="everyone",
                cited_value=(
                    "required for every person, for non-console access. Accounts "
                    "running automated functions are outside it. A sign-in with "
                    "phishing-resistant factors alone is enough."
                ),
                source_id="PCI DSS v4.0.1",
                clause="8.4.2",
                link=_PCI,
                date_checked="2026-11-19",
            ),
            _cited(
                "ftc_financial_institution",
                direction="level",
                bound="everyone",
                cited_value=_EVERYONE,
                source_id="16 CFR 314",
                clause="314.4(c)(5)",
                link=_CFR,
                date_checked="2026-11-10",
                written_alternative=True,
            ),
            _cited(
                "dfs_covered_entity",
                direction="level",
                bound="everyone",
                cited_value=_EVERYONE,
                source_id="23 NYCRR 500",
                clause="500.12",
                link=_NYCRR,
                date_checked="2026-11-10",
                written_alternative=True,
            ),
            _cited(
                "hipaa",
                direction="level",
                bound="everyone",
                cited_value=_EVERYONE,
                source_id="HIPAA Security Rule NPRM",
                clause="RIN 0945-AA22",
                link=_HIPAA,
                date_checked="2026-11-22",
                status="proposed",
            ),
            _cited(
                "non_corporate_commonwealth_entity",
                direction="level",
                bound="everyone",
                cited_value=_EVERYONE,
                source_id="Essential Eight (Nov 2023)",
                clause="Maturity Level Two, required by PSPF Release 2026 requirement 0101",
                link=_E8,
                date_checked="2026-11-22",
            ),
        ),
    ),
    ReportCheck(
        "settings.retention",
        SETTINGS.id,
        "audit.read",
        "The audit trail is kept for a period that is in force.",
        (
            _cited(
                "dfs_covered_entity",
                direction="floor",
                bound=365 * 3,
                cited_value=(
                    "at least 3 years (1,095 days), for trails of cybersecurity "
                    "events. A longer period in the same clause covers financial "
                    "transactions, which this trail does not hold"
                ),
                source_id="23 NYCRR 500",
                clause="500.6(b)",
                link=_NYCRR,
                date_checked="2026-11-10",
            ),
            _cited(
                "federal_tax_information",
                direction="floor",
                bound=365 * 7,
                cited_value="at least 7 years (2,555 days)",
                source_id="IRS Publication 1075",
                clause="AU-11",
                link=_IRS,
                date_checked="2026-11-19",
            ),
        ),
    ),
    ReportCheck(
        "settings.model_processing",
        SETTINGS.id,
        "platform.read",
        "Every provider a model component's calls reach meets each in-force "
        "value for the selection, by the operator's Provider Statements. "
        "Model calls only.",
        (
            _cited(
                "hipaa",
                direction="statement",
                bound="hipaa",
                cited_value=(
                    "self-hosted, or a BAA signed with the provider and its "
                    "HIPAA-eligible account mode stated"
                ),
                source_id="45 CFR 164.308(b)",
                clause=(
                    "164.308(b)(1) for a covered entity or (b)(2) for a business "
                    "associate, each with (b)(3)"
                ),
                link=_BA,
                date_checked="2026-11-22",
            ),
            _cited(
                "federal_tax_information",
                direction="statement",
                bound="fti",
                cited_value=(
                    "processing and support region stated as the United States "
                    "and, for a provider that isn't self-hosted, a FedRAMP "
                    "authorization stated"
                ),
                source_id="IRS Publication 1075 (Rev. 11-2021)",
                clause="§2.C.7, §3.3.1",
                link=_IRS,
                date_checked="2026-11-19",
            ),
            # GDPR names no kind: the EU alone scopes it.
            ScopedValue(
                ("european_union",),
                (),
                "statement",
                "gdpr",
                (
                    "processing region stated in the EU/EEA, or a transfer basis "
                    "stated"
                ),
                "GDPR (Regulation (EU) 2016/679); Decision (EU) 2023/1795",
                "Arts 44–46, 49; Art. 1 (appeal C-703/25 P pending)",
                f"{_GDPR}; {_DPF}",
                "2026-11-19",
            ),
        ),
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
    ReportCheck(
        "site.crown_jewel_seen",
        SITE.id,
        SITE.permission,
        "Every crown jewel a read source can see was heard from within 7 days of the read. A never-connected agent is listed, not heard.",
    ),
    ReportCheck(
        "site.crown_jewel_place",
        SITE.id,
        SITE.permission,
        "Every host of a seen crown jewel sits in the zone it names. An unseen crown jewel is not checked for place.",
    ),
    ReportCheck(
        "site.owner",
        SITE.id,
        SITE.permission,
        "Every zone and crown jewel has an owner naming an active team.",
    ),
    ReportCheck(
        "site.undeclared",
        SITE.id,
        SITE.permission,
        "Every host a read source lists falls in some zone. A fail gives a count per source, never a host.",
    ),
    ReportCheck(
        "production.running.service",
        RUNNING.id,
        RUNNING.permission,
        "The background service is running. Off is not checked: nothing is collected.",
    ),
    ReportCheck(
        "production.running.triage",
        RUNNING.id,
        RUNNING.permission,
        "Automatic triage is on in this process. When the setting was not supplied, the check is not checked.",
    ),
    ReportCheck(
        "production.running.source",
        RUNNING.id,
        RUNNING.permission,
        "At least one alert source is switched on, the same way Day 1 visibility judges it. With none, the check needs a person.",
    ),
    ReportCheck(
        "production.quiet",
        QUIET.id,
        QUIET.permission,
        "A switched-on alert source passes when a new alert arrived in the window's last 24 hours. None in that day, or none ever, needs a person. An unhealthy source needs a person. The check names the record it read.",
    ),
)

MODULES = {
    VISIBILITY.id: VISIBILITY,
    SETTINGS.id: SETTINGS,
    COVERAGE.id: COVERAGE,
    ESTATE.id: ESTATE,
    SITE.id: SITE,
    HISTORY_BEFORE.id: HISTORY_BEFORE,
    HISTORY_RECHECK.id: HISTORY_RECHECK,
    HISTORY_CAPPED.id: HISTORY_CAPPED,
    HISTORY_TUNING.id: HISTORY_TUNING,
    FORECAST.id: FORECAST,
    RUNNING.id: RUNNING,
    QUIET.id: QUIET,
    ALERTS.id: ALERTS,
    TUNING.id: TUNING,
    CONFIDENCE.id: CONFIDENCE,
    AGREEMENT.id: AGREEMENT,
    SPEND.id: SPEND,
    CHANGES.id: CHANGES,
    TIME.id: TIME,
}


def spec_for(check_id: str) -> ReportCheck:
    """The registry entry a produced check id belongs to."""
    for spec in CHECKS:
        if check_id == spec.id or check_id.startswith(spec.id + "."):
            return spec
    raise KeyError(check_id)


def scope_label(value: ScopedValue) -> str:
    """The source's own scope, in catalogue sentences."""
    if not value.jurisdictions and not value.kinds:
        return "every install"
    jurisdictions = {row.id: row.sentence for row in JURISDICTIONS}
    kinds = {row.id: row.sentence for row in KINDS}
    words = [jurisdictions[item] for item in value.jurisdictions]
    words.extend(kinds[item] for item in value.kinds)
    return "; ".join(words)


def _held(value: ScopedValue, jurisdictions: set[str], kinds: set[str]) -> bool:
    return set(value.jurisdictions) <= jurisdictions and set(value.kinds) <= kinds


def _in_force(
    values: tuple[ScopedValue, ...],
    jurisdictions: set[str],
    kinds: set[str],
    include,
) -> list[ScopedValue]:
    """In-force values the selection holds in full.

    A scoped value replaces the every-install one even when the bounds are equal.
    """
    held = [
        value
        for value in values
        if value.status == "in_force"
        and include(value)
        and _held(value, jurisdictions, kinds)
    ]
    scoped = [value for value in held if value.jurisdictions or value.kinds]
    if scoped:
        return scoped
    return held


def _names(value: ScopedValue, selected: set[str]) -> int:
    return len((set(value.jurisdictions) | set(value.kinds)) & selected)


def _tighter_bound(candidate: ScopedValue, best: ScopedValue) -> bool:
    if candidate.direction != best.direction:
        return False
    if candidate.direction == "ceiling":
        return int(candidate.bound) < int(best.bound)
    if candidate.direction == "floor":
        return int(candidate.bound) > int(best.bound)
    if candidate.direction == "level":
        return _LEVEL_RANK[str(candidate.bound)] > _LEVEL_RANK[str(best.bound)]
    raise RuntimeError(candidate.direction)


def _same_bound(candidate: ScopedValue, best: ScopedValue) -> bool:
    return candidate.direction == best.direction and candidate.bound == best.bound


def _pick(
    candidates: list[ScopedValue], selected: set[str], tighter, same
) -> Optional[ScopedValue]:
    """Tightest bound. A tie names more of the selection, else registry order."""
    best: Optional[ScopedValue] = None
    for candidate in candidates:
        if best is None or tighter(candidate, best):
            best = candidate
            continue
        if tighter(best, candidate) or not same(candidate, best):
            continue
        if _names(candidate, selected) > _names(best, selected):
            best = candidate
    return best


def applicable(
    check_id: str, jurisdictions: list[str], kinds: list[str]
) -> tuple[ScopedValue, ...]:
    """Every in-force value of a statement check the selection holds in full.

    Each one applies: a provider has to meet all of them.
    """
    chosen_j, chosen_k = set(jurisdictions), set(kinds)
    return tuple(
        value
        for value in spec_for(check_id).values
        if value.status == "in_force" and _held(value, chosen_j, chosen_k)
    )


def resolve(check_id: str, jurisdictions: list[str], kinds: list[str]) -> Resolved:
    """The Applicable Value for one settings check, plus shown-only parts."""
    spec = spec_for(check_id)
    chosen_j = set(jurisdictions)
    chosen_k = set(kinds)
    selected = chosen_j | chosen_k
    values = spec.values
    scored = _pick(
        _in_force(values, chosen_j, chosen_k, lambda value: True),
        selected,
        _tighter_bound,
        _same_bound,
    )
    window = _pick(
        _in_force(
            values,
            chosen_j,
            chosen_k,
            lambda value: value.window_minutes is not None,
        ),
        selected,
        lambda candidate, best: int(candidate.window_minutes or 0)
        < int(best.window_minutes or 0),
        lambda candidate, best: candidate.window_minutes == best.window_minutes,
    )
    gap = _pick(
        _in_force(
            values, chosen_j, chosen_k, lambda value: value.release_gap is not None
        ),
        selected,
        lambda candidate, best: False,
        lambda candidate, best: True,
    )
    proposed = tuple(
        value
        for value in values
        if value.status == "proposed" and _held(value, chosen_j, chosen_k)
    )
    return Resolved(scored, proposed, window, gap)
