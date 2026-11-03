"""Link inventory rules that are copies of one rule in different places.

The inventory keeps one row per copy: the LSASS dump rule in the customer's
repo and the same rule running in Splunk are two rules. This finds those pairs
and writes them to ``rule_links``. Matchers, most certain first:

- ``same_id``: two sources list the rule under the same own id (Kibana keeps a
  repo rule's ``rule_id``; a fork of a Sigma repo keeps the Sigma id).
- ``vigil_tag``: a copy carries ``vigil:R-n`` anywhere in it (tag, description,
  comment), naming the rule it came from.
- ``known_name``: Splunk lists ESCU rule ``<name>`` as ``ESCU - <name> - Rule``.
- ``looks_same``: a SIEM rule whose logic values (the strings and numbers it
  tests for) are mostly a repo rule's, as after converting Sigma by hand. A
  suggestion only: it stays ``pending`` until a person confirms it.

The first three are ``auto``. A copy has one origin, so a SIEM rule that already
has an ``auto`` link is not compared by logic, and one that is gets only its
best repo rule (ties kept). Rules Wazuh ships are nobody's copy and are skipped.

A run writes only new pairs, and turns a ``pending`` pair ``auto`` when a
certain matcher now finds it. It never deletes a link or changes one a person
confirmed or rejected, so a rerun with nothing new writes nothing.
"""

from __future__ import annotations

import json
import logging
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

from sqlalchemy import select, text, update
from sqlalchemy.dialects.postgresql import insert

from core.storage.connection import get_db_manager
from core.storage.models import LINK_HOWS, RuleLink

logger = logging.getLogger(__name__)

SIEM_SOURCES = ("splunk", "elastic", "wazuh")
# Where Wazuh keeps the rules it ships; a customer's own are under etc/rules.
WAZUH_SHIPPED = "wazuh:ruleset/"

# A rule's own id, as opposed to a repo path a file without an id falls back
# to, a Splunk "<app>/<name>" key, or a Wazuh rule number: no slash or space,
# at least one letter, no rule-file suffix.
_OWN_ID = re.compile(r"^(?=.*[A-Za-z])[^\s/]{8,}$")
_FILE_SUFFIX = re.compile(r"\.(ya?ml|toml|kql|md|txt|json|xml)$", re.IGNORECASE)

ESCU_NAME = re.compile(r"^ESCU - (.+) - Rule$")

# Postgres regex. The tag may sit in any field, so the whole content is read.
VIGIL_TAG_SQL = r"(?:^|[^A-Za-z0-9_])vigil:(R-[0-9]+)(?![0-9])"

# looks_same: at least this share of the two rules' values in common, and at
# least this many values. One shared value is coincidence, not a copy.
LOOKS_SAME_MIN_SCORE = 0.6
LOOKS_SAME_MIN_SHARED = 2
# Repo rules tied for best that one SIEM rule is suggested for.
LOOKS_SAME_MAX_TIED = 3
# A value in more repo rules than this does not pick candidates (it still
# counts toward the score once a pair is a candidate).
_COMMON_VALUE = 300

# Fields that say where to search, not what to look for. A Sigma rule keeps
# them in logsource, outside its detection, so they are dropped from every query.
_WHERE_FIELDS = frozenset(
    {
        "index",
        "sourcetype",
        "source",
        "host",
        "earliest",
        "latest",
        "datamodel",
        "summariesonly",
        "allow_old_summaries",
        "event.category",
        "event.type",
        "event.kind",
        "event.dataset",
        "event.module",
        "data_stream.dataset",
        "host.os.type",
    }
)
# Sigma detection keys that are structure, not values.
_SIGMA_STRUCTURE = frozenset({"condition", "timeframe"})
# Wazuh rule options that chain or tune a rule rather than say what it matches.
_WAZUH_STRUCTURE = frozenset(
    {
        "if_sid",
        "if_group",
        "if_level",
        "if_matched_sid",
        "if_matched_group",
        "frequency",
        "timeframe",
        "options",
        "group",
        "same_source_ip",
        "same_field",
        "different_field",
    }
)
# A Wazuh pattern's regex syntax, so "(?i)\\procdump\.exe$" compares as the
# value it matches.
_REGEX_FLAGS = re.compile(r"\(\?[a-z]+\)")
_REGEX_GROUPING = re.compile(r"[()^$]")

_SPL_COMMENT = re.compile(r"```.*?```", re.DOTALL)
_MACRO = re.compile(r"`[^`]*`")
_PAIR = re.compile(
    r"""([A-Za-z_][\w.\-]*)\s*(?:==|!=|=|:)\s*("(?:[^"\\]|\\.)*"|[^\s"()|,\[\]]+)"""
)
_QUOTED = re.compile(r'"((?:[^"\\]|\\.)*)"')
_EDGE = "*%? \t\\/"


def own_id(native_key: str) -> bool:
    """Whether a native key is the rule's own id, which a copy may keep."""
    return bool(_OWN_ID.match(native_key)) and not _FILE_SUFFIX.search(native_key)


def _norm(value: Any) -> Optional[str]:
    if value is None or isinstance(value, bool):
        return None
    out = str(value).lower().replace("\\\\", "\\").strip(_EDGE)
    return out if len(out) >= 3 else None


def _sigma_values(value: Any) -> Iterable[Any]:
    if isinstance(value, dict):
        for key, inner in value.items():
            if str(key).lower() not in _SIGMA_STRUCTURE:
                yield from _sigma_values(inner)
    elif isinstance(value, (list, tuple)):
        for inner in value:
            yield from _sigma_values(inner)
    else:
        yield value


def _wazuh_values(details: Dict[str, Any]) -> Iterable[str]:
    """Each matched value of a Wazuh rule, with its regex syntax taken off."""
    for key, value in details.items():
        if key in _WAZUH_STRUCTURE:
            continue
        for item in value if isinstance(value, list) else [value]:
            pattern = item.get("pattern") if isinstance(item, dict) else item
            if not isinstance(pattern, str):
                continue
            pattern = _REGEX_GROUPING.sub("", _REGEX_FLAGS.sub("", pattern))
            for part in pattern.split("|"):
                yield part.replace("\\.", ".").replace("\\\\", "\\")


def _query_values(query: str) -> Iterable[str]:
    """Quoted strings and field=value / field:value values, minus where-to-search fields."""
    text_ = _MACRO.sub(" ", _SPL_COMMENT.sub(" ", query))
    for match in _PAIR.finditer(text_):
        if match.group(1).lower() in _WHERE_FIELDS:
            text_ = text_.replace(match.group(0), " ", 1)
    for match in _PAIR.finditer(text_):
        if not match.group(2).startswith('"'):
            yield match.group(2)
    for match in _QUOTED.finditer(text_):
        yield match.group(1)


def logic_values(query: str, language: str) -> frozenset:
    """What a rule tests for, normalised so a hand conversion compares equal."""
    if not query:
        return frozenset()
    raw: Iterable[Any]
    try:
        if language == "sigma":
            raw = list(_sigma_values(json.loads(query)))
        elif language == "wazuh-xml":
            raw = list(_wazuh_values(json.loads(query)))
        else:
            raw = _query_values(query)
    except (ValueError, AttributeError):
        raw = _query_values(query)
    return frozenset(v for v in (_norm(x) for x in raw) if v)


@dataclass
class _Rule:
    id: str
    name: str
    source: str
    native_key: str
    language: str
    query: str
    seen_in: str

    @property
    def in_repo(self) -> bool:
        return self.source.startswith("repo:")

    @property
    def shipped(self) -> bool:
        """A rule the SIEM ships with (Wazuh's ruleset): nobody's copy."""
        return self.source == "wazuh" and self.seen_in.startswith(WAZUH_SHIPPED)


@dataclass
class Proposal:
    how: str
    confirmed: str
    evidence: Dict[str, Any]
    score: Optional[float] = None


@dataclass
class LinkReport:
    rules: int = 0
    found: Dict[str, int] = field(default_factory=dict)
    new: Dict[str, int] = field(default_factory=dict)
    upgraded: int = 0
    unchanged: int = 0
    tags_unresolved: List[str] = field(default_factory=list)
    names_ambiguous: List[str] = field(default_factory=list)
    writes: int = 0


def _pair(a: str, b: str) -> Tuple[str, str]:
    return (a, b) if a < b else (b, a)


def _propose(
    found: Dict[Tuple[str, str], Proposal], a: str, b: str, proposal: Proposal
) -> None:
    key = _pair(a, b)
    held = found.get(key)
    if held is None or LINK_HOWS.index(proposal.how) < LINK_HOWS.index(held.how):
        found[key] = proposal


def match_same_id(rules: List[_Rule], found, report: LinkReport) -> None:
    by_key: Dict[str, List[_Rule]] = defaultdict(list)
    for rule in rules:
        if own_id(rule.native_key):
            by_key[rule.native_key].append(rule)
    for key, group in by_key.items():
        for i, a in enumerate(group):
            for b in group[i + 1 :]:
                # Same language too: Sigma has reused an Elastic rule's id for
                # a different rule, and a copy keeps its language.
                if a.source != b.source and a.language == b.language:
                    _propose(
                        found,
                        a.id,
                        b.id,
                        Proposal("same_id", "auto", {"id": key}),
                    )


def match_vigil_tag(
    rules: Dict[str, _Rule], tags: List[Tuple[str, str]], found, report: LinkReport
) -> None:
    for carrier_id, target_id in tags:
        carrier, target = rules.get(carrier_id), rules.get(target_id)
        if carrier is None or target_id == carrier_id:
            continue
        if target is None:
            report.tags_unresolved.append(f"{carrier_id}->{target_id}")
            continue
        if carrier.source == target.source:
            continue
        _propose(
            found,
            carrier.id,
            target.id,
            Proposal(
                "vigil_tag", "auto", {"tag": f"vigil:{target_id}", "in": carrier_id}
            ),
        )


def match_known_name(rules: List[_Rule], found, report: LinkReport) -> None:
    repo_spl: Dict[str, Dict[str, List[_Rule]]] = defaultdict(lambda: defaultdict(list))
    for rule in rules:
        if rule.in_repo and rule.language == "spl":
            repo_spl[rule.name][rule.source].append(rule)
    for rule in rules:
        if rule.source != "splunk":
            continue
        match = ESCU_NAME.match(rule.name)
        if not match:
            continue
        for source, hits in repo_spl.get(match.group(1), {}).items():
            if len(hits) > 1:
                report.names_ambiguous.append(f"{rule.id}:{source}")
                continue
            _propose(
                found,
                rule.id,
                hits[0].id,
                Proposal("known_name", "auto", {"name": match.group(1)}),
            )


def match_looks_same(rules: List[_Rule], found, report: LinkReport) -> None:
    certain: Set[str] = set()
    for a, b in found:
        certain.update((a, b))

    repo = [r for r in rules if r.in_repo]
    repo_values = [logic_values(r.query, r.language) for r in repo]
    index: Dict[str, List[int]] = defaultdict(list)
    for i, values in enumerate(repo_values):
        for value in values:
            index[value].append(i)

    for rule in rules:
        if rule.source not in SIEM_SOURCES or rule.id in certain or rule.shipped:
            continue
        values = logic_values(rule.query, rule.language)
        if len(values) < LOOKS_SAME_MIN_SHARED:
            continue
        shared: Counter = Counter()
        for value in values:
            hits = index.get(value, ())
            if len(hits) <= _COMMON_VALUE:
                shared.update(hits)
        scored: List[Tuple[float, int]] = []
        for i in shared:
            both = values & repo_values[i]
            if len(both) < LOOKS_SAME_MIN_SHARED:
                continue
            score = len(both) / len(values | repo_values[i])
            if score >= LOOKS_SAME_MIN_SCORE:
                scored.append((score, i))
        if not scored:
            continue
        # A copy has one origin: only the best repo rule, across every repo.
        scored.sort(key=lambda s: (-s[0], repo[s[1]].id))
        top = scored[0][0]
        for score, i in scored[:LOOKS_SAME_MAX_TIED]:
            if score < top:
                break
            both = sorted(values & repo_values[i])
            _propose(
                found,
                rule.id,
                repo[i].id,
                Proposal(
                    "looks_same",
                    "pending",
                    {"shared": both[:20], "shared_count": len(both)},
                    round(score, 3),
                ),
            )


def _load(session) -> Tuple[List[_Rule], List[Tuple[str, str]]]:
    rows = session.execute(text("""
            SELECT r.id, r.name, r.source, r.native_key,
                   coalesce(v.content->>'language', ''), coalesce(v.content->>'query', ''),
                   v.seen_in
            FROM rules r
            JOIN rule_versions v ON v.rule_id = r.id AND v.version = r.latest_version
            """)).all()
    rules = [_Rule(*row) for row in rows]
    # Every version: a tag a copy was shipped with still names its origin.
    tags = session.execute(
        text("""
            SELECT DISTINCT v.rule_id, upper(m[1])
            FROM rule_versions v
            CROSS JOIN LATERAL regexp_matches(v.content::text, :pattern, 'gi') AS m
            """),
        {"pattern": VIGIL_TAG_SQL},
    ).all()
    return rules, [tuple(t) for t in tags]


def find_links(session, report: LinkReport) -> Dict[Tuple[str, str], Proposal]:
    rules, tags = _load(session)
    report.rules = len(rules)
    found: Dict[Tuple[str, str], Proposal] = {}
    match_same_id(rules, found, report)
    match_vigil_tag({r.id: r for r in rules}, tags, found, report)
    match_known_name(rules, found, report)
    match_looks_same(rules, found, report)
    report.found = dict(Counter(p.how for p in found.values()))
    return found


def link_all() -> LinkReport:
    """Find every link in the inventory and write the new ones."""
    report = LinkReport()
    with get_db_manager().session_scope() as session:
        found = find_links(session, report)
        held = {
            (a, b): confirmed
            for a, b, confirmed in session.execute(
                select(RuleLink.rule_a, RuleLink.rule_b, RuleLink.confirmed)
            ).all()
        }
        new_rows = []
        for (a, b), p in sorted(found.items()):
            state = held.get((a, b))
            if state is None:
                new_rows.append(
                    {
                        "rule_a": a,
                        "rule_b": b,
                        "how": p.how,
                        "confirmed": p.confirmed,
                        "score": p.score,
                        "evidence": p.evidence,
                    }
                )
                report.new[p.how] = report.new.get(p.how, 0) + 1
            elif state == "pending" and p.confirmed == "auto":
                session.execute(
                    update(RuleLink)
                    .where(RuleLink.rule_a == a, RuleLink.rule_b == b)
                    .values(
                        how=p.how, confirmed="auto", score=None, evidence=p.evidence
                    )
                )
                report.upgraded += 1
            else:
                report.unchanged += 1
        if new_rows:
            session.execute(
                insert(RuleLink).on_conflict_do_nothing(
                    index_elements=["rule_a", "rule_b"]
                ),
                new_rows,
            )
        report.writes = len(new_rows) + report.upgraded
    return report


def decide(rule_a: str, rule_b: str, by: str, accept: bool) -> bool:
    """A person confirms or rejects a pending link. False when there is none pending."""
    a, b = _pair(rule_a, rule_b)
    with get_db_manager().session_scope() as session:
        result = session.execute(
            update(RuleLink)
            .where(
                RuleLink.rule_a == a,
                RuleLink.rule_b == b,
                RuleLink.confirmed == "pending",
            )
            .values(
                confirmed="confirmed" if accept else "rejected",
                confirmed_by=by,
                confirmed_at=text("now()"),
            )
        )
        return result.rowcount == 1


def main(argv: List[str]) -> int:
    from dataclasses import asdict

    from core.storage.connection import init_database

    init_database()
    report = link_all()
    row = asdict(report)
    row["tags_unresolved"] = row["tags_unresolved"][:10]
    row["names_ambiguous"] = row["names_ambiguous"][:10]
    print(json.dumps(row))
    return 0


if __name__ == "__main__":
    import sys

    sys.exit(main(sys.argv[1:]))
