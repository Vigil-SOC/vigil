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

`ref_body` is ported too: an empty `${NAME:-}` or `${NAME-}` stays, and a
non-empty default becomes `${NAME:-[REDACTED]}` / `${NAME-[REDACTED]}`.
`${NAME:?…}` and `${NAME:+…}` stay whole.

Shapes redact.awk does not have: Cookie / Set-Cookie values (a header line, and
httpcore's `b'set-cookie', b'…'` trace), Redis `AUTH`, URL-encoded `password%3D`,
JWTs whose header is not `eyJ`, `client_secret <value>` with no separator,
`-p <password>` after `mysql`, `mysqladmin`, or `redis-cli`, Bearer values under
23 characters, URL passwords containing a space, and `requirepass`
(`--requirepass` and `requirepass=`).

Out of scope (K2 cuts): indentation-aware YAML block scalars (the rest of the
text is dropped instead) and the support bundle's never() list.
"""

from __future__ import annotations

import re2

from services.medic.redact.secret_names import NAMES

REDACTED = "[REDACTED]"
# Bumped whenever a rule changes what is redacted; stamped on every observation.
REDACTION_VERSION = "k2min-2"

_PEM = re2.compile(
    r"(?s)-----BEGIN [A-Z ]*PRIVATE KEY( BLOCK)?-----.*?"
    r"(-----END [A-Z ]*PRIVATE KEY( BLOCK)?-----|\z)"
)
_URL = re2.compile(r"[A-Za-z][A-Za-z0-9+.-]*://[^/@ \t\"'?#:]*:[^@\t\"'?#\r\n]*@")
_KEY = re2.compile(r"[A-Za-z_][A-Za-z0-9_.-]*(\\)?[\"']?[ \t]*(?::|=|%3[Dd])")
_FLAG = re2.compile(r"--[A-Za-z][A-Za-z0-9_.-]*")
_CONTEXT_TOKEN = re2.compile(r"(?i)\btoken[ \t]+(\S+)")
# A short plain word after "token" is prose ("token expired"), not a token.
_PROSE_WORD = re2.compile(r"[a-z]{1,12}[.,;:!)]?")
_BLOCK = re2.compile(r"[|>][-+0-9]*")
_WRAPPER = re2.compile(r"[bBuUrRfF]|[A-Za-z]*\(")
# (pattern, replacement, minimum match length), in redact.awk's order.
# Bearer is its own step: the 23-character floor stays for any token, and a
# shorter one is still redacted unless it is a prose word ("authentication",
# "realm"). That is what keeps the two support-bundle edge lines readable.
_SHAPES = [
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


_NAMES = frozenset(n.replace("_", "").replace("-", "").lower() for n in NAMES)

# A pure lowercase word after Bearer is prose, up to the old 23-character floor
# ("Bearer " + 16). These two sit under that floor and are not tokens.
_BEARER = re2.compile(r"[Bb][Ee][Aa][Rr][Ee][Rr][ \t]+([A-Za-z0-9._~+/=-]+)")
_BEARER_PROSE = frozenset({"authentication", "realm"})
_COOKIE_HDR = re2.compile(
    r"(?i)((?:^|[^A-Za-z0-9_]))((?:set-)?cookie)([ \t]*:[ \t]*)([^\r\n]*)"
)
# httpcore logs headers as (b'name', b'value'); RE2 has no backreferences.
_COOKIE_REPR = re2.compile(r"""(?i)b(['"])((?:set-)?cookie)['"],\s*b(['"])""")
_P_CMD = re2.compile(r"(?i)\b(?:mysqladmin|mysql|redis-cli)\b")
_AUTH = re2.compile(r"\bAUTH[ \t]+(\S+)(?:[ \t]+(\S+))?")
_CLIENT_SECRET = re2.compile(r"(?i)\bclient[_-]?secret\b[ \t]+(\S+)")
# Three base64url segments. An uppercase letter keeps versions and hostnames
# (1.2.3, api.example.com, 10.0.0.5) out; those are lowercase.
_JWT = re2.compile(r"[A-Za-z0-9_-]{4,}\.[A-Za-z0-9_-]{4,}\.[A-Za-z0-9_-]{4,}")


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
    # Not in secret-names.txt (that file is a pinned copy). Redis --requirepass.
    if flat == "requirepass":
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


def _kept_ref(body: str) -> tuple[str, int] | None:
    """Port of redact.awk `ref_body`.

    None means "not a reference to keep": the caller redacts the whole value.
    A returned pair is (text to emit, hits). An empty default is not a hit.
    """
    if re2.fullmatch(r"\$[A-Za-z_][A-Za-z0-9_]*", body):
        return body, 0
    if re2.fullmatch(r"\$\{[A-Za-z_][A-Za-z0-9_]*(:-|-|:\+|:\?)?.*\}", body) is None:
        return None
    inner = body[2:-1]
    name = re2.match(r"[A-Za-z_][A-Za-z0-9_]*", inner).group(0)
    rest = inner[len(name) :]
    if rest == "" or re2.match(r":[+?]", rest):
        return body, 0
    if re2.match(r":?-", rest) is None:
        return None
    op = ":-" if rest.startswith(":") else "-"
    default = rest[len(op) :]
    if default == "" or default == REDACTED:
        return body, 0
    if "${" in default or "$(" in default:
        return None
    return "${" + name + op + REDACTED + "}", 1


def _bearer_is_prose(value: str, match_len: int) -> bool:
    """True when a Bearer match must be left alone.

    The old 23-character floor still redacts every longer token. Under it, a
    short lowercase word is prose ("Bearer authentication", "Bearer realm=").
    A trailing "=" is the WWW-Authenticate param, not part of the word.
    """
    if match_len >= 23:
        return False
    core = value.removesuffix("=")
    return re2.fullmatch(r"[a-z]+", core) is not None and (
        len(core) < 8 or core in _BEARER_PROSE
    )


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
            self._cookies,
            self._keys,
            self._flags,
            self._short_p,
            self._auth,
            self._client_secret,
            self._bearer,
            self._shapes,
            self._jwt,
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
        if ":" not in s and "=" not in s and "%3D" not in s and "%3d" not in s:
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
            if body.startswith("$") and (ref := _kept_ref(body)) is not None:
                # Consume the reference. Leaving it in `s` rescans `NAME:` inside
                # ${NAME:?…} and redacts the operator text.
                repl, extra = ref
                done += pre + repl + post
                s = rest
                n += extra
                continue
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
            if _is_blank(body) or (not sep and body.startswith("-")):
                continue
            if body.startswith("$") and (ref := _kept_ref(body)) is not None:
                repl, extra = ref
                done += sep + pre + repl + post
                s = rest
                n += extra
                continue
            done += sep + pre + REDACTED + post
            s = rest
            n += 1
        return done + s, n

    @staticmethod
    def _cookies(s: str) -> tuple[str, int]:
        if "ookie" not in s and "OOKIE" not in s:
            return s, 0
        s, n = Redactor._cookie_repr(s)
        s, n2 = Redactor._cookie_headers(s)
        return s, n + n2

    @staticmethod
    def _cookie_kept(value: str) -> bool:
        stripped = value.strip()
        return stripped == "" or stripped == REDACTED or stripped.startswith(REDACTED)

    @staticmethod
    def _cookie_headers(s: str) -> tuple[str, int]:
        n = 0
        out: list[str] = []
        pos = 0
        for m in _COOKIE_HDR.finditer(s):
            if Redactor._cookie_kept(m.group(4)):
                continue
            out.append(
                s[pos : m.start()] + m.group(1) + m.group(2) + m.group(3) + REDACTED
            )
            pos = m.end()
            n += 1
        out.append(s[pos:])
        return "".join(out), n

    @staticmethod
    def _cookie_repr(s: str) -> tuple[str, int]:
        n = 0
        out: list[str] = []
        pos = 0
        for m in _COOKIE_REPR.finditer(s):
            if m.start() < pos:
                continue
            q = m.group(3)
            vstart = m.end()
            vend = vstart
            while vend < len(s) and s[vend] not in "\r\n":
                if s[vend] == "\\":
                    vend += 2
                    continue
                if s[vend] == q:
                    break
                vend += 1
            if Redactor._cookie_kept(s[vstart:vend]):
                continue
            out.append(s[pos:vstart] + REDACTED)
            pos = vend
            n += 1
        out.append(s[pos:])
        return "".join(out), n

    @staticmethod
    def _short_p(s: str) -> tuple[str, int]:
        # -p <password> only after mysql, mysqladmin, or redis-cli. A bare -p
        # (mkdir -p, docker run -p, ssh -p, vigil -p) is left alone.
        if "-p" not in s or _P_CMD.search(s) is None:
            return s, 0
        n = 0
        out: list[str] = []
        pos = 0
        for cmd in _P_CMD.finditer(s):
            if cmd.start() < pos:
                continue
            line_end = s.find("\n", cmd.end())
            if line_end < 0:
                line_end = len(s)
            j = s.find("-p", cmd.end(), line_end)
            while 0 <= j < line_end:
                prev = s[j - 1] if j else ""
                nxt = s[j + 2 : j + 3]
                if prev == "-" or _isword(prev) or nxt not in ("", " ", "\t"):
                    j = s.find("-p", j + 2, line_end)
                    continue
                k = j + 2
                while k < line_end and s[k] in " \t":
                    k += 1
                pre, body, post, rest = _take(s[k:], 0)
                end = len(s) - len(rest)
                if k >= line_end or _is_blank(body) or body.startswith("-"):
                    j = s.find("-p", j + 2, line_end)
                    continue
                if body.startswith("$") and (ref := _kept_ref(body)) is not None:
                    repl, extra = ref
                    if extra:
                        out.append(s[pos:k] + pre + repl + post)
                        pos = end
                        n += extra
                    j = s.find("-p", end, line_end)
                    continue
                out.append(s[pos:k] + pre + REDACTED + post)
                pos = end
                n += 1
                # Search from `end`. Starting at end+2 skips a -p that sits
                # in the single space after this password.
                j = s.find("-p", end, line_end)
        out.append(s[pos:])
        return "".join(out), n

    @staticmethod
    def _auth(s: str) -> tuple[str, int]:
        # Redis AUTH takes a password, or a username and a password.
        if "AUTH" not in s:
            return s, 0
        n = 0
        out: list[str] = []
        pos = 0
        for m in _AUTH.finditer(s):
            if m.start() < pos:
                continue
            tokens = [t for t in (m.group(1), m.group(2)) if t]
            if all(t.startswith(REDACTED) for t in tokens):
                continue
            repl = " ".join(t if t.startswith(REDACTED) else REDACTED for t in tokens)
            # Keep the command and the whitespace that followed it.
            head = s[m.start() : m.start(1)]
            out.append(s[pos : m.start()] + head + repl)
            pos = m.end()
            n += 1
        out.append(s[pos:])
        return "".join(out), n

    @staticmethod
    def _client_secret(s: str) -> tuple[str, int]:
        if "secret" not in s and "Secret" not in s and "SECRET" not in s:
            return s, 0
        n = 0
        out: list[str] = []
        pos = 0
        for m in _CLIENT_SECRET.finditer(s):
            if m.start() < pos:
                continue
            value = m.group(1)
            if _is_blank(value) or value.startswith("-"):
                continue
            if value.startswith("$") and (ref := _kept_ref(value)) is not None:
                repl, extra = ref
                if extra:
                    out.append(s[pos : m.start(1)] + repl)
                    pos = m.end()
                    n += extra
                continue
            out.append(s[pos : m.start(1)] + REDACTED)
            pos = m.end()
            n += 1
        out.append(s[pos:])
        return "".join(out), n

    @staticmethod
    def _bearer(s: str) -> tuple[str, int]:
        if "earer" not in s and "EARER" not in s:
            return s, 0
        n = 0
        out: list[str] = []
        pos = 0
        for m in _BEARER.finditer(s):
            if m.start() < pos or _word_before(s[: m.start()]):
                continue
            value = m.group(1)
            if _bearer_is_prose(value, m.end() - m.start()):
                continue
            out.append(s[pos : m.start()] + "Bearer " + REDACTED)
            pos = m.end()
            n += 1
        out.append(s[pos:])
        return "".join(out), n

    @staticmethod
    def _jwt(s: str) -> tuple[str, int]:
        if s.count(".") < 2:
            return s, 0
        n = 0
        out: list[str] = []
        pos = 0
        for m in _JWT.finditer(s):
            if m.start() < pos:
                continue
            start, end = m.start(), m.end()
            if _word_before(s[:start]) or (start and s[start - 1] == "."):
                continue
            if end < len(s) and (s[end] == "." or _isword(s[end])):
                continue
            if re2.search(r"[A-Z]", m.group(0)) is None:
                continue
            out.append(s[pos:start] + REDACTED)
            pos = end
            n += 1
        out.append(s[pos:])
        return "".join(out), n

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
