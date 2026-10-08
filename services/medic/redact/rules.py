"""Pattern-only secret redaction, ported from the support bundle's redact.awk.

Medic holds no secret values, so `redact.awk`'s exact-value list can't be used
(K1 T-01). What is ported (scripts/vigil-support/redact.awk at 9959a69a):

- key names: `KEY=value`, `"key": "value"`, `key: value` for a credential-shaped
  name or one in `secret-names.txt` (`secret_key`, `redact_keys`);
- URL credentials: `scheme://user:pass@host` keeps scheme, user and host;
- PEM private-key blocks, including one with no END (redacted to the end);
- token shapes (`Bearer`, JWT, provider key prefixes) with their minimum lengths;
- long flags: `--db-password value`, `--api-key=value`;

plus the contextual `token <value>` rule K1 T-01 adds in case the reset-token
format changes. Every secret becomes the fixed string `[REDACTED]`: no length, no
fragment. Regexes run on google-re2 (linear time, A3-1); the parsing around them is
plain Python, as it is plain awk in the original.

Out of scope here (full K2): indentation-aware YAML block scalars (the rest of the
text is dropped instead), `${X:-default}`
defaults, and flag names redact.awk doesn't know (e.g. `--requirepass`).
"""

from __future__ import annotations

from pathlib import Path

import re2

REDACTED = "[REDACTED]"
# Bumped whenever a rule changes what is redacted; stamped on every observation.
REDACTION_VERSION = "k2min-1"

_PEM = re2.compile(
    r"(?s)-----BEGIN [A-Z ]*PRIVATE KEY( BLOCK)?-----.*?"
    r"(-----END [A-Z ]*PRIVATE KEY( BLOCK)?-----|\z)"
)
_URL = re2.compile(r"[A-Za-z][A-Za-z0-9+.-]*://[^/@ \t\"'?#:]*:[^@ \t\"'?#]*@")
_KEY = re2.compile(r"[A-Za-z_][A-Za-z0-9_.-]*(\\)?[\"']?[ \t]*[:=]")
_FLAG = re2.compile(r"--[A-Za-z][A-Za-z0-9_.-]*")
_CONTEXT_TOKEN = re2.compile(r"(?i)\btoken[ \t]+(\S+)")
# A short plain word after "token" is prose ("token expired"), not a token.
_PROSE_WORD = re2.compile(r"[a-z]{1,12}[.,;:!)]?")
_BLOCK = re2.compile(r"[|>][-+0-9]*")
_WRAPPER = re2.compile(r"[bBuUrRfF]|[A-Za-z]*\(")
_REF = re2.compile(
    r"\$[A-Za-z_][A-Za-z0-9_]*|\$\{[A-Za-z_][A-Za-z0-9_]*(:[+?][^}]*)?\}"
)

# (pattern, replacement, minimum match length), in redact.awk's order.
_SHAPES = [
    (
        re2.compile(r"[Bb][Ee][Aa][Rr][Ee][Rr][ \t]+[A-Za-z0-9._~+/=-]+"),
        "Bearer " + REDACTED,
        23,
    ),
    (re2.compile(r"eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]*"), REDACTED, 0),
    (re2.compile(r"sk-[A-Za-z0-9_-]+"), REDACTED, 32),
    (re2.compile(r"xai-[A-Za-z0-9]+"), REDACTED, 20),
    (re2.compile(r"gsk_[A-Za-z0-9]+"), REDACTED, 20),
    (re2.compile(r"glpat-[A-Za-z0-9_-]+"), REDACTED, 20),
    (re2.compile(r"AIza[A-Za-z0-9_-]+"), REDACTED, 30),
    (re2.compile(r"A[KS]IA[A-Z0-9]+"), REDACTED, 20),
    (re2.compile(r"gh[pousr]_[A-Za-z0-9]+"), REDACTED, 20),
    (re2.compile(r"github_pat_[A-Za-z0-9_]+"), REDACTED, 30),
    (re2.compile(r"xox[a-z]-[A-Za-z0-9-]+"), REDACTED, 20),
    (re2.compile(r"xapp-[A-Za-z0-9-]+"), REDACTED, 20),
    (re2.compile(r"[sr]k_(live|test)_[A-Za-z0-9]+"), REDACTED, 20),
    (re2.compile(r"(npm|hf)_[A-Za-z0-9]+"), REDACTED, 20),
    (re2.compile(r"SG\.[A-Za-z0-9_.-]+"), REDACTED, 30),
    (re2.compile(r"(pdus\+_|u\+)[A-Za-z0-9_+-]+"), REDACTED, 20),
]

_SUFFIXES = (
    "key",
    "secret",
    "token",
    "password",
    "passphrase",
    "dsn",
    "passwd",
    "pwd",
    "webhook_url",
)
# Credential-shaped names that are settings, not secrets (redact.awk's allow list).
_ALLOW = frozenset(
    {
        "auth_min_password_length",
        "auth_max_password_bytes",
        "password_reset_ttl_seconds",
        "existing_secret",
        "existing_secret_key",
        "existing_secret_password_key",
        "user_password_key",
    }
)


def _load_names() -> frozenset[str]:
    text = (Path(__file__).parent / "secret-names.txt").read_text()
    return frozenset(
        line.strip().replace("_", "").replace("-", "").lower()
        for line in text.splitlines()
        if line.strip()
    )


_NAMES = _load_names()


def _isword(c: str) -> bool:
    return c.isascii() and (c.isalnum() or c == "_")


def _snake(key: str) -> str:
    """camelCase, kebab-case and UPPER_SNAKE all become lower_snake."""
    out = []
    for i, c in enumerate(key):
        p = key[i - 1] if i else ""
        nx = key[i + 1] if i + 1 < len(key) else ""
        if c in "-.":
            c = "_"
        if (
            c.isupper()
            and p
            and (p.islower() or p.isdigit() or (p.isupper() and nx.islower()))
        ):
            out.append("_")
        out.append(c.lower())
    return "".join(out)


def secret_key(key: str) -> bool:
    s = _snake(key)
    if s in _ALLOW:
        return False
    flat = s.replace("_", "")
    if flat in _NAMES or flat.endswith("authorization"):
        return True
    if flat.endswith(("password", "passphrase", "secret", "apikey", "token")) or s in (
        "dsn",
        "passwd",
        "webhook_url",
    ):
        return True
    return any(s.endswith("_" + suf) for suf in _SUFFIXES)


def _word_before(ctx: str) -> bool:
    """Is ctx glued to a word character on its right edge? JSON's \\n and \\t end a word."""
    if not ctx:
        return False
    prev = ctx[-1]
    if len(ctx) >= 2 and ctx[-2] == "\\" and prev in "ntr":
        return False
    return _isword(prev)


def _is_blank(body: str) -> bool:
    return (
        body in ("", "null", "None", "~", "true", "false")
        or body.startswith(REDACTED)
        or body.endswith(REDACTED)
    )


def _take(s: str, mode: int) -> tuple[str, str, str, str]:
    """Split s into (pre, body, post, rest). mode 0: an unquoted value ends at a
    blank, '&' or quote; 1: at a quote; 2: at the end of the text."""
    lead = s[: len(s) - len(s.lstrip(" \t"))]
    rest = s[len(lead) :]
    q = '\\"' if rest.startswith('\\"') else rest[:1]
    if q in ('\\"', '"', "'"):
        rest = rest[len(q) :]
        i = 0
        while i < len(rest):
            if rest[i] == "\\" and q != '\\"':
                i += 2
                continue
            if q == "'" and rest[i : i + 2] == "''":
                i += 2
                continue
            if rest.startswith(q, i):
                return lead + q, rest[:i], q, rest[i + len(q) :]
            i += 1
        return lead + q, rest, "", ""
    if mode == 2:
        end = len(rest) if "\n" not in rest else rest.index("\n")
        return lead, rest[:end], "", rest[end:]
    i = 0
    while i < len(rest):
        c = rest[i]
        if c in "\"'" or (c == "\\" and rest[i + 1 : i + 2] in ('"', "'")):
            break
        if c in "\r\n" or (mode == 0 and c in " \t&"):
            break
        i += 1
    body = rest[:i]
    # b'x', u"x" and SecretStr('x'): the quote opens the value, not ends it.
    if mode < 2 and i < len(rest) and _WRAPPER.fullmatch(body):
        pre, body, post, after = _take(rest[i:], mode)
        return lead + rest[:i] + pre, body, post, after
    return lead, body, "", rest[i:]


class Redactor:
    """`redact(text)` → (redacted text, number of replacements). Stateless."""

    def redact(self, text: str) -> tuple[str, int]:
        hits = 0
        for step in (
            self._pem,
            self._urls,
            self._keys,
            self._flags,
            self._shapes,
            self._context,
        ):
            text, n = step(text)
            hits += n
        return text, hits

    @staticmethod
    def _pem(s: str) -> tuple[str, int]:
        return _PEM.subn(REDACTED, s)

    @staticmethod
    def _urls(s: str) -> tuple[str, int]:
        if "://" not in s:
            return s, 0
        n = 0

        def repl(m: re2.Match) -> str:
            nonlocal n
            text = m.group(0)
            scheme_end = text.index("://") + 3
            user, _, password = text[scheme_end:-1].partition(":")
            if not password or password == REDACTED:
                return text
            n += 1
            return f"{text[:scheme_end]}{user}:{REDACTED}@"

        return _URL.sub(repl, s), n

    @staticmethod
    def _keys(s: str) -> tuple[str, int]:
        if ":" not in s and "=" not in s:
            return s, 0
        done, n, pending = "", 0, False
        while (m := _KEY.search(s)) is not None:
            prefix = done + s[: m.start()]
            tok, s = m.group(0), s[m.end() :]
            done = prefix + tok
            key = re2.match(r"[A-Za-z0-9_.-]+", tok).group(0)
            if prefix.endswith("/"):
                continue  # a path component
            sk = secret_key(key)
            if _snake(key) == "name":
                # `name: SMTP_PASSWORD` then `value: …` (Kubernetes env lists).
                pending = secret_key(_take(s, 0)[1])
                continue
            if _snake(key) == "value" and pending:
                sk, pending = True, False
            if not sk:
                continue
            line = prefix.rsplit("\n", 1)[-1]
            at_start = (
                re2.fullmatch(r"[ \t]*(-[ \t]+)?(export[ \t]+)?", line) is not None
            )
            mode = 2 if at_start else (1 if _snake(key) == "authorization" else 0)
            pre, body, post, rest = _take(s, mode)
            if (
                _is_blank(body)
                or body in ("[", "{")
                or body.startswith("{{")
                or s.startswith("//")
            ):
                continue
            if _REF.fullmatch(body):
                continue  # a variable reference, not a value
            if not pre.endswith(("'", '"')) and _BLOCK.fullmatch(body):
                # A YAML block scalar: the value is on the lines below. Unlike
                # redact.awk this doesn't track indentation; it drops the rest.
                return done + pre + body + "\n" + REDACTED, n + 1
            done += pre + REDACTED + post
            s = rest
            n += 1
        return done + s, n

    @staticmethod
    def _flags(s: str) -> tuple[str, int]:
        if "--" not in s:
            return s, 0
        done, n = "", 0
        while (m := _FLAG.search(s)) is not None:
            ctx = done + s[: m.start()]
            flag, s = m.group(0), s[m.end() :]
            done = ctx + flag
            c, prev = s[:1], ctx[-1:]
            if (
                _isword(prev)
                or prev == "-"
                or c not in ("=", " ", "\t")
                or not secret_key(flag[2:])
            ):
                continue
            sep = "=" if c == "=" else ""
            pre, body, post, rest = _take(s[len(sep) :], 0)
            if (
                _is_blank(body)
                or (not sep and body.startswith("-"))
                or _REF.fullmatch(body)
            ):
                continue
            done += sep + pre + REDACTED + post
            s = rest
            n += 1
        return done + s, n

    @staticmethod
    def _shapes(s: str) -> tuple[str, int]:
        n = 0
        for pattern, repl, minlen in _SHAPES:
            out, pos = [], 0
            for m in pattern.finditer(s):
                start, end = m.start(), m.end()
                if end - start < minlen or _word_before(s[:start]):
                    continue
                out.append(s[pos:start] + repl)
                pos = end
                n += 1
            out.append(s[pos:])
            s = "".join(out)
        return s, n

    @staticmethod
    def _context(s: str) -> tuple[str, int]:
        n = 0

        def repl(m: re2.Match) -> str:
            nonlocal n
            value = m.group(1)
            if value.startswith((REDACTED, "--")) or _PROSE_WORD.fullmatch(value):
                return m.group(0)
            n += 1
            return m.group(0)[: m.start(1) - m.start(0)] + REDACTED

        return _CONTEXT_TOKEN.sub(repl, s), n
