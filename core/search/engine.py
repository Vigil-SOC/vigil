"""Find things in Vigil by what they say and by what they mean.

Every searchable item (an alert, a case, a workflow, an agent, a skill) becomes a
``Doc``. A query scores each doc two ways and keeps the better:

* words: each query word found in the title or body, a word's start counting
  (``exfil`` finds ``exfiltration``), plus the security words that mean the same
  (``data theft`` finds ``exfiltration``, ``password dump`` finds ``lsass``) at a
  lower weight, so a query in plain language still lands without a model;
* meaning: cosine of the query against the doc on an embedding model, when one
  answers. Doc vectors are cached by content, filled in the background, so the
  first search is not held up by embedding everything. With no model the search
  is words only.

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
from array import array
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Tuple

import httpx

logger = logging.getLogger(__name__)

_WORD = re.compile(r"[a-z0-9][a-z0-9._@-]*[a-z0-9]|[a-z0-9]")
STOP = frozenset(
    "a an and are as at be by for from has have in is it its of on or that the this to was were with "
    "show me find all any my our what which who where when how list get give see look about".split()
)

# Words analysts use for the same thing. A query word that names a concept (or
# one of its words) brings the rest in at a lower weight. Aliases are specific
# on purpose: "data" or "logon" alone would pull in everything.
CONCEPTS: Dict[str, str] = {
    "exfiltration": "exfil, exfiltrate, exfiltrated, data theft, stolen data, data leak, leaked, egress, large upload, t1041, t1048, t1567",
    "phishing": "phish, phished, lure, malicious attachment, macro, docm, spearphishing, t1566",
    "lateral movement": "lateral, psexec, winrm, admin shares, remote desktop, pass-the-hash, wmi, t1021, t1570",
    "credential access": "credential, credentials, stolen credentials, stolen password, credential dumping, password dump, dumping, lsass, mimikatz, ntds, security account manager, kerberoast, t1003, t1558, valid accounts, compromised account, account takeover, t1078",
    "brute force": "brute, failed logon, logon_failed, password spray, spraying, guessing, lockout, 4625, bad password, mfa denied, t1110",
    "command and control": "c2, beacon, beaconing, callback, implant, cobalt, t1071, t1095",
    "ransomware": "ransom, encrypt, encrypted, vssadmin, shadow copies, inhibit system recovery, t1486, t1490",
    "persistence": "run key, registry run, autorun, scheduled task, startup folder, new service, t1547, t1053, t1543",
    "privilege escalation": "privilege, elevation, escalate, uac bypass, sudo, t1068, t1548",
    "discovery": "recon, reconnaissance, port scan, scanning, enumeration, nmap, sweep, t1046, t1087, t1018",
    "execution": "powershell, encoded command, wscript, mshta, command shell, t1059",
    "defense evasion": "evasion, obfuscated, obfuscation, tamper, disable defender, clear logs, t1562, t1070, t1027",
    "suspicious sign-in": "sign-in, signin, login, logins, logging, location, travel, impossible travel, unfamiliar location, mfa fatigue, risky sign-in, entra, okta",
    "malware": "virus, trojan, pup, adware, quarantined, infected",
    "containment": "contain, isolate, isolation, quarantine, revoke session, network contain",
}
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
MEANING_FLOOR = 0.4
MEANING_BAND = 0.2
# Routine alerts repeat by the hundred, so a band alone still floods: past this many
# per kind, meaning alone is left out.
MEANING_ONLY_MAX = 10
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
    return [a.strip() for a in CONCEPTS[concept].split(",") if a.strip()]


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
        digest = hashlib.sha1(self.text.encode(), usedforsecurity=False).hexdigest()
        return f"{self.kind}:{self.id}:{digest[:12]}"


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
    total = 0.0
    found: List[str] = []
    missing = 0.0
    for w in own:
        s = _says(w, title, body, title_text, body_text)
        if s:
            found.append(w)
            total += s * weight.get(w, 1.0)
        else:
            missing += weight.get(w, 1.0)
    via = ""
    if missing and related:
        # words the doc lacks may be said another way: the best concept stands in for them
        said, via = max(
            (
                (
                    max(
                        (_says(a, title, body, title_text, body_text) for a in alias),
                        default=0.0,
                    ),
                    c,
                )
                for c, alias in related.items()
            ),
            key=lambda x: x[0],
        )
        if said:
            standin = (
                STANDIN_TITLE * said / TITLE
                if said > BODY
                else STANDIN_BODY * said / BODY
            )
            total += standin * TITLE * missing
        else:
            via = ""
    score = total / (TITLE * sum(weight.get(w, 1.0) for w in own))
    if len(own) > 1 and phrase and phrase in doc.text.lower():
        score += 0.25
    shown = [w for w in found if weight.get(w, 1.0) >= 0.5] or found
    if found and not missing:
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


def ollama_embedder(base_url: str, model: str, timeout: float = 8.0) -> Embedder:
    """Embeddings from an Ollama /api/embed, or None when it or the model is missing."""

    def embed(texts: List[str]) -> Optional[List[List[float]]]:
        try:
            r = httpx.post(
                f"{base_url.rstrip('/')}/api/embed",
                json={"model": model, "input": texts},
                timeout=timeout,
            )
            r.raise_for_status()
            got = r.json().get("embeddings")
        except Exception as exc:  # noqa: BLE001 - any failure means words only
            logger.info("search: embeddings unavailable (%s)", exc)
            return None
        if not isinstance(got, list) or len(got) != len(texts):
            return None
        return got

    return embed


def _cosine(a: Sequence[float], b: Sequence[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


class VectorCache:
    """Doc vectors by content key, filled a batch at a time off the request path.

    Bounded: at most ``max_entries`` vectors, kept as float32, the oldest
    dropped first. A doc whose text changes gets a new key, so the stale
    vector ages out rather than piling up.
    """

    def __init__(
        self,
        embed: Optional[Embedder],
        batch: int = 48,
        cooldown: float = 60.0,
        max_entries: int = 10_000,
    ):
        self.embed = embed
        self.batch = batch
        self.cooldown = cooldown
        self.max_entries = max_entries
        self._vectors: "OrderedDict[str, array]" = OrderedDict()
        self._down_until = 0.0
        self._lock = threading.Lock()
        self._filling = False

    @property
    def available(self) -> bool:
        return self.embed is not None and time.monotonic() >= self._down_until

    def __len__(self) -> int:
        return len(self._vectors)

    def get(self, key: str) -> Optional[array]:
        return self._vectors.get(key)

    def _put(self, key: str, vector: Sequence[float]) -> None:
        with self._lock:
            self._vectors[key] = array("f", vector)
            self._vectors.move_to_end(key)
            while len(self._vectors) > self.max_entries:
                self._vectors.popitem(last=False)

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
        todo = [d for d in docs if d.key not in self._vectors]
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
                        self._put(d.key, v)
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
    if cache is not None and cache.available and not idlike:
        qvec = cache.query(q)
    else:
        qvec = None
    if cache is not None:
        cache.fill(docs)
    method = "embeddings" if qvec else "words"
    meaning: Dict[str, float] = {}
    if qvec is not None and cache is not None:
        for d in docs:
            v = cache.get(d.key)
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
        score = max(ws, ms * MEANING) + 0.25 * min(ws, ms)
        if score < min_score:
            continue
        if ws >= ms * MEANING:
            by = "both" if ms > 0.3 else "words"
        else:
            by = "both" if ws > 0.15 else "meaning"
            why = why if by == "both" else "similar meaning"
        hits.append(Hit(d, round(score, 3), why or "similar meaning", by))
    # ties go to the newer item
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
