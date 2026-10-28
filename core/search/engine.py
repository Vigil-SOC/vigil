"""Find things in Vigil by what they say and by what they mean.

Every searchable item (an alert, a case, a workflow, an agent, a skill) becomes a
``Doc``. A query scores each doc two ways and keeps the better:

* words: each query word found in the title or body, a word's start counting
  (``exfil`` finds ``exfiltration``), plus the security words that mean the same
  (``data theft`` finds ``exfiltration``, ``password dump`` finds ``lsass``) at a
  lower weight, so a query in plain language still lands without a model;
* meaning: cosine of the query against the doc on an embedding model, when one
  answers. Doc vectors are cached by content, filled in the background, so the
  first search is not held up by embedding everything.

Each hit says why it matched, so a result that does not contain the words reads
as deliberate rather than random.
"""

from __future__ import annotations

import hashlib
import logging
import math
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

_WORD = re.compile(r"[a-z0-9][a-z0-9._@-]*[a-z0-9]|[a-z0-9]")
STOP = frozenset(
    "a an and are as at be by for from has have in is it its of on or that the this to was were with "
    "show me find all any my our what which who where when how list get give see look about".split()
)

# Words analysts use for the same thing, and the Logs search that finds it in
# telemetry. A query word that names a concept (or one of its words) brings the
# rest in at EXPANDED weight. Aliases are specific on purpose: "data" or "logon"
# alone would pull in everything.
CONCEPTS: Dict[str, Tuple[str, str]] = {
    "exfiltration": (
        "exfil, exfiltrate, exfiltrated, data theft, stolen data, data leak, data upload, leaked, egress, large upload, t1041, t1048, t1567",
        "exfil,t1041,t1048,t1567",
    ),
    "phishing": (
        "phish, phished, lure, malicious attachment, macro, docm, spearphishing, t1566",
        "phish,t1566,docm",
    ),
    "lateral movement": (
        "lateral, psexec, winrm, admin shares, remote desktop, pass-the-hash, wmi, t1021, t1570",
        "lateral,winrm,psexec,t1021,t1570",
    ),
    "credential access": (
        "credential, credentials, stolen credentials, stolen password, credential dumping, password dump, dumping, lsass, mimikatz, ntds, security account manager, kerberoast, t1003, t1558, valid accounts, compromised account, account takeover, t1078",
        "t1078,t1003,t1558,lsass,mimikatz,kerberoast",
    ),
    "brute force": (
        "brute, failed logon, logon_failed, password spray, spraying, guessing, lockout, 4625, bad password, mfa denied, t1110",
        "action:logon_failed,signin_failed",
    ),
    "command and control": (
        "c2, beacon, beaconing, callback, implant, cobalt, t1071, t1095",
        "beacon,t1071,c2",
    ),
    "ransomware": (
        "ransom, encrypt, encrypted, vssadmin, shadow copies, inhibit system recovery, t1486, t1490",
        "ransom,vssadmin,t1486,t1490",
    ),
    "persistence": (
        "run key, registry run, autorun, scheduled task, startup folder, new service, t1547, t1053, t1543",
        "t1547,t1053,t1543,autorun",
    ),
    "privilege escalation": (
        "privilege, elevation, escalate, uac bypass, sudo, t1068, t1548",
        "t1068,t1548,privilege",
    ),
    "discovery": (
        "recon, reconnaissance, port scan, scanning, enumeration, nmap, sweep, t1046, t1087, t1018",
        "scan,t1046,t1087,t1018",
    ),
    "execution": (
        "powershell, encoded command, wscript, mshta, command shell, t1059",
        "powershell,t1059",
    ),
    "defense evasion": (
        "evasion, obfuscated, obfuscation, tamper, disable defender, clear logs, t1562, t1070, t1027",
        "t1562,t1070,t1027,tamper",
    ),
    "suspicious sign-in": (
        "sign-in, signin, login, logins, logging, impossible travel, mfa fatigue, risky sign-in, entra, okta",
        "action:signin_failed,signin",
    ),
    # Where a sign-in came from is its own question: "from a weird place" is not
    # answered by every blocked legacy sign-in.
    "unusual location": (
        "weird place, strange place, odd place, unusual place, new location, abroad, unfamiliar location, location, new country, "
        "foreign country, country, impossible travel, travel, geolocation, geo",
        "action:signin_failed,signin",
    ),
    "malware": (
        "virus, trojan, pup, adware, quarantined, infected",
        "action:quarantined",
    ),
    "containment": (
        "contain, isolate, isolation, quarantine, revoke session, network contain",
        "action:quarantined",
    ),
}
EXPANDED = 0.55
TITLE = 3.0
BODY = 1.0
# How fully a concept's other wording stands in for the query words a doc lacks:
# said in the title, nearly as good as the words themselves; in the body, still a
# clear answer, since alerts often name the technique only in their description.
STANDIN_TITLE = 0.85
STANDIN_BODY = 0.6
# Meaning alone ranks below words at the same strength, and must be close to the
# best meaning match, so a loose resemblance never floods the results.
MEANING = 0.75
MEANING_FLOOR = 0.45
MEANING_BAND = 0.2
# Routine alerts repeat by the hundred, so a band alone still floods: past this many
# per kind, meaning alone is left out.
MEANING_ONLY_MAX = 10
# A query that names a security concept ("ransomware") says what counts: an alert,
# case or skill that only resembles it is noise. Workflows and agents that handle
# it may still be offered by meaning.
MEANING_ONLY_FOR_CONCEPTS = frozenset({"workflow", "agent"})
# Within a kind, a hit under this share of the kind's best score is not counted:
# 600 routine sign-ins that touch a concept should not read as 600 answers.
RELATIVE = 0.5
# Ids, hosts, addresses and accounts ("INC-1042", "cr-14", "10.0.10.20", "f.ortiz") are looked up, not
# interpreted: an embedding of an id resembles every other id.
_IDLIKE = re.compile(r"^(?=[^ ]*[\d.@])[a-z0-9]+(?:[-_.:/@][a-z0-9]+)+$")


def is_idlike(query: str) -> bool:
    return bool(_IDLIKE.match(query.strip().lower()))


def words(text: str) -> List[str]:
    return [w for w in _WORD.findall((text or "").lower()) if w not in STOP]


def _stem(w: str) -> str:
    for suffix in ("ations", "ation", "ings", "ing", "ed", "es", "s"):
        if len(w) > len(suffix) + 3 and w.endswith(suffix):
            return w[: -len(suffix)]
    return w


def _aliases(concept: str) -> List[str]:
    return [a.strip() for a in CONCEPTS[concept][0].split(",") if a.strip()]


def expand(query: str) -> Tuple[List[str], Dict[str, List[str]]]:
    """The query's own words, and for each concept it names, the other ways to say it."""
    own = words(query)
    joined = " ".join(own)
    stems = {_stem(w) for w in own}
    related: Dict[str, List[str]] = {}
    for concept in CONCEPTS:
        names = [concept] + _aliases(concept)
        hit = any(
            n in joined
            or (
                " " not in n
                and (
                    _stem(n) in stems
                    or any(len(w) >= 4 and n.startswith(w) for w in own)
                )
            )
            for n in names
        )
        if hit:
            related[concept] = [n for n in names if n not in own]
    return own, related


def _named_by(concept: str, own: List[str]) -> Tuple[set, set]:
    """The query words that name a concept, and those that name it only as part of a phrase."""
    joined = " ".join(own)
    stems = {w: _stem(w) for w in own}
    by_word: set = set()
    by_phrase: set = set()
    for n in [concept] + _aliases(concept):
        if " " in n:
            if n in joined:
                by_phrase |= set(words(n)) & set(own)
            continue
        for w in own:
            if (
                n == w
                or n in w
                or _stem(n) == stems[w]
                or (len(w) >= 4 and n.startswith(w))
            ):
                by_word.add(w)
    return by_word | by_phrase, by_phrase - by_word


def logs_query(concept: str) -> str:
    return CONCEPTS[concept][1]


@dataclass
class Doc:
    kind: str  # alert | case | workflow | agent | skill
    id: str
    title: str
    body: str = ""
    subtitle: str = ""
    route: Dict[str, str] = field(default_factory=dict)
    when: Optional[str] = None

    @property
    def text(self) -> str:
        return f"{self.title}. {self.body}".strip()

    @property
    def key(self) -> str:
        return (
            f"{self.kind}:{self.id}:{hashlib.sha1(self.text.encode()).hexdigest()[:12]}"
        )


@dataclass
class Hit:
    doc: Doc
    score: float
    why: str
    by: str  # words | meaning | both | id


def _best(token: str, toks: Sequence[str]) -> float:
    """1 for the word itself (or its stem), 0.8 for a word it starts, else 0."""
    st = _stem(token)
    best = 0.0
    for t in toks:
        if t == token or _stem(t) == st:
            return 1.0
        if len(token) >= 3 and t.startswith(token):
            best = 0.8
    return best


def _says(
    phrase: str, title: List[str], body: List[str], title_text: str, body_text: str
) -> float:
    """How strongly a doc says a word (by its tokens) or a phrase (by its text)."""
    if " " in phrase:
        return TITLE if phrase in title_text else BODY if phrase in body_text else 0.0
    return max(_best(phrase, title) * TITLE, _best(phrase, body) * BODY)


def word_score(
    doc: Doc,
    own: List[str],
    related: Dict[str, List[str]],
    phrase: str,
    weight: Optional[Dict[str, float]] = None,
) -> Tuple[float, str]:
    """0-1 for how well the doc's words answer the query, and the reason in words.

    ``weight`` lowers words most docs contain ("data", "activity"), so they
    cannot carry a result on their own when the query says more.
    """
    if not own:
        return 0.0, ""
    weight = weight or {}
    title_text = doc.title.lower()
    body_text = f"{doc.body} {doc.subtitle}".lower()
    title = words(title_text)
    body = words(body_text)
    names = {c: _named_by(c, own) for c in related}
    # A word that is only part of a concept's phrase ("access" in "credential access",
    # "theft" in "data theft") is not the concept: alone it does not count.
    phrase_only = {
        w
        for c, (_, part) in names.items()
        if part
        and not any(
            _says(n, title, body, title_text, body_text)
            for n in [c] + _aliases(c)
            if " " in n and n in " ".join(own)
        )
        for w in part
    }
    total = 0.0
    found: List[str] = []
    lacking: List[str] = []
    for w in own:
        s = 0.0 if w in phrase_only else _says(w, title, body, title_text, body_text)
        if s:
            found.append(w)
            total += s * weight.get(w, 1.0)
        else:
            lacking.append(w)
    via = ""
    if lacking and related:
        # words the doc lacks may be said another way: each concept stands in for the
        # words that name it, and the other words are covered only as well as the
        # doc covers every concept the query names
        cover: Dict[str, float] = {}
        for c, alias in related.items():
            said = max(
                (_says(a, title, body, title_text, body_text) for a in alias),
                default=0.0,
            )
            cover[c] = (
                STANDIN_TITLE * said / TITLE
                if said > BODY
                else STANDIN_BODY * said / BODY
            )
        weakest = min(cover.values())
        used: Dict[str, float] = {}
        for w in lacking:
            mine = [c for c in related if w in names[c][0]]
            if mine:
                c = max(mine, key=lambda k: cover[k])
                standin = cover[c]
                # the reason names the concept that answered the most query words
                used[c] = used.get(c, 0.0) + standin * weight.get(w, 1.0)
            else:
                standin = weakest
            total += standin * TITLE * weight.get(w, 1.0)
        if any(used.values()):
            via = max(used, key=lambda k: used[k])
    score = total / (TITLE * sum(weight.get(w, 1.0) for w in own))
    if len(own) > 1 and phrase and phrase in doc.text.lower():
        score += 0.25
    shown = [w for w in found if weight.get(w, 1.0) >= 0.5] or found
    if found and not lacking:
        why = "mentions " + ", ".join(f"“{w}”" for w in shown[:3])
    elif via:
        why = f"about {via}" + (
            f", mentions “{shown[0]}”"
            if shown and weight.get(shown[0], 1.0) >= 0.5
            else ""
        )
    elif found:
        why = "mentions " + ", ".join(f"“{w}”" for w in shown[:3])
    else:
        why = ""
    return min(score, 1.0), why


def common_word_weights(own: List[str], docs: List[Doc]) -> Dict[str, float]:
    """1 for a word few docs contain, down to 0.2 for one most of them do."""
    if len(docs) < 20:
        return {}
    out = {}
    for w in own:
        n = sum(1 for d in docs if _best(w, words(d.text)) > 0)
        frac = n / len(docs)
        out[w] = 1.0 if frac <= 0.1 else max(0.2, 1.0 - (frac - 0.1) * 1.6)
    return out


Embedder = Callable[[List[str]], Optional[List[List[float]]]]


def _cosine(a: Sequence[float], b: Sequence[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


class VectorCache:
    """Doc vectors by content key, filled a batch at a time off the request path."""

    def __init__(
        self, embed: Optional[Embedder], batch: int = 48, cooldown: float = 60.0
    ):
        self.embed = embed
        self.batch = batch
        self.cooldown = cooldown
        self.vectors: Dict[str, List[float]] = {}
        self._down_until = 0.0
        self._lock = threading.Lock()
        self._filling = False

    @property
    def available(self) -> bool:
        return self.embed is not None and time.monotonic() >= self._down_until

    def _call(self, texts: List[str]) -> Optional[List[List[float]]]:
        if not self.available:
            return None
        out = self.embed(texts) if self.embed else None
        if out is None:
            self._down_until = time.monotonic() + self.cooldown
        return out

    def query(self, text: str) -> Optional[List[float]]:
        got = self._call([f"search_query: {text}"])
        return got[0] if got else None

    def fill(self, docs: Iterable[Doc], wait: bool = False) -> None:
        todo = [d for d in docs if d.key not in self.vectors]
        if not todo or not self.available:
            return
        with self._lock:
            if self._filling:
                return
            self._filling = True

        def run() -> None:
            try:
                for i in range(0, len(todo), self.batch):
                    chunk = todo[i : i + self.batch]
                    vecs = self._call(
                        [f"search_document: {d.text[:2000]}" for d in chunk]
                    )
                    if not vecs:
                        return
                    for d, v in zip(chunk, vecs):
                        self.vectors[d.key] = v
            finally:
                self._filling = False

        if wait:
            run()
        else:
            threading.Thread(target=run, name="search-embed", daemon=True).start()


def meaning_score(cos: float) -> float:
    """Embedding cosines bunch up; spread the useful band (about 0.5-0.8) over 0-1."""
    return max(0.0, min(1.0, (cos - 0.5) / 0.28))


def search(
    query: str,
    docs: List[Doc],
    cache: Optional[VectorCache] = None,
    *,
    min_score: float = 0.2,
) -> Tuple[List[Hit], str]:
    """Docs best first, and how meaning was judged: an embedding model or word lists."""
    q = query.strip()
    ql = q.lower()
    idlike = is_idlike(q)
    own, related = expand(q)
    weight = common_word_weights(own, docs)
    qvec = cache.query(q) if cache and cache.available and not idlike else None
    if cache:
        cache.fill(docs)
    method = "embeddings" if qvec else "words"
    meaning: Dict[str, float] = {}
    if qvec is not None and cache is not None:
        for d in docs:
            v = cache.vectors.get(d.key)
            if v is not None:
                meaning[d.key] = meaning_score(_cosine(qvec, v))
    top = max(meaning.values(), default=0.0)
    floor = max(MEANING_FLOOR, top - MEANING_BAND)
    hits: List[Hit] = []
    for d in docs:
        did = d.id.lower()
        if did == ql:
            hits.append(Hit(d, 2.0, "its id", "id"))
            continue
        if idlike and len(ql) >= 4 and did.startswith(ql):
            hits.append(Hit(d, 1.5, "its id starts with it", "id"))
            continue
        ws, why = word_score(d, own, related, ql, weight)
        ms = meaning.get(d.key, 0.0)
        if ws < min_score and ms < floor:
            continue  # resemblance alone, and not among the closest
        if ws < min_score and related and d.kind not in MEANING_ONLY_FOR_CONCEPTS:
            continue  # the concept's own words decide what an alert or case is about
        score = max(ws, ms * MEANING) + 0.25 * min(ws, ms)
        if score < min_score:
            continue
        if ws >= ms * MEANING:
            by = "both" if ms > 0.3 else "words"
        else:
            by = "both" if ws > 0.15 else "meaning"
            why = why if by == "both" else "similar meaning"
        hits.append(Hit(d, round(score, 3), why or "similar meaning", by))
    best: Dict[str, float] = {}
    for h in hits:
        if h.by != "id":
            best[h.doc.kind] = max(best.get(h.doc.kind, 0.0), h.score)
    hits = [h for h in hits if h.by == "id" or h.score >= RELATIVE * best[h.doc.kind]]
    hits.sort(key=lambda h: (h.score, h.doc.when or ""), reverse=True)
    alone: Dict[str, int] = {}
    kept: List[Hit] = []
    for h in hits:
        if h.by == "meaning":
            alone[h.doc.kind] = alone.get(h.doc.kind, 0) + 1
            if alone[h.doc.kind] > MEANING_ONLY_MAX:
                continue
        kept.append(h)
    return kept, method
