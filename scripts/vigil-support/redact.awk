#!/usr/bin/awk -f
# Support-bundle redaction filter: stdin to stdout, POSIX awk (gawk, mawk, BSD).
#
#   awk -f redact.awk -v names=secret-names.txt [-v values=exact.txt] \
#       [-v counts=counts.tsv -v name=<label>] [-v learn=1] < in > out
#
# names   one secret env-var name per line (required; the filter fails closed
#         without it). Matched case-insensitively with "_" and "-" ignored, so
#         POSTGRES_PASSWORD, postgresPassword and postgres-password all hit.
# values  optional file of exact values learned elsewhere, one per line. Each of
#         6+ characters is replaced wherever it appears (fixed-string match).
# counts  optional side file; "<name><TAB><replacements>" is appended at the end.
# learn   with learn=1 nothing is redacted or passed through: the output is the
#         values to feed back as `values`, one per line. Those are the values of
#         credential-class fields (a name ending in password, passphrase, secret,
#         token, key, apikey, dsn, passwd or pwd, or webhook_url) and the password
#         of any scheme://user:pass@host URL, from lines that are not comments.
#
# Every secret becomes the fixed string [REDACTED]: no length, no fragment.

function fail(msg) {
    print "redact.awk: " msg > "/dev/stderr"
    failed = 1
    exit 2
}

function isupper(c) { return c != "" && index("ABCDEFGHIJKLMNOPQRSTUVWXYZ", c) > 0 }
function islower(c) { return c != "" && index("abcdefghijklmnopqrstuvwxyz", c) > 0 }
function isdigit(c) { return c != "" && index("0123456789", c) > 0 }
function isword(c) { return c != "" && (isupper(c) || islower(c) || isdigit(c) || c == "_") }

# camelCase, kebab-case and UPPER_SNAKE all become lower_snake.
function snake(k,   i, c, p, nx, out) {
    out = ""
    for (i = 1; i <= length(k); i++) {
        c = substr(k, i, 1)
        p = i > 1 ? substr(k, i - 1, 1) : ""
        nx = substr(k, i + 1, 1)
        if (c == "-" || c == ".") c = "_"
        if (isupper(c) && p != "" && (islower(p) || isdigit(p) || (isupper(p) && islower(nx))))
            out = out "_"
        out = out tolower(c)
    }
    return out
}

function endswith(s, suf) {
    return length(s) >= length(suf) && substr(s, length(s) - length(suf) + 1) == suf
}

function secret_key(k,   s, flat, i) {
    s = snake(k)
    if (s in allow) return 0
    flat = s
    gsub(/_/, "", flat)
    if (flat in names_set || endswith(flat, "authorization")) return 1
    # Run-together names (PGPASSWORD, apikey) too. A bare "key" is too common a
    # word, so it only counts after an underscore, below.
    if (endswith(flat, "password") || endswith(flat, "passphrase") || endswith(flat, "secret") ||
        endswith(flat, "apikey") || endswith(flat, "token") || s == "dsn" ||
        s == "passwd" || s == "webhook_url") return 1
    for (i = 1; i <= nsuffix; i++)
        if (endswith(s, "_" suffix[i])) return 1
    return 0
}

# Names whose values are credentials, as against registry names such as a
# username or client ID that are redacted by name but are ordinary words elsewhere.
function cred_class(k,   s, flat) {
    if (!secret_key(k)) return 0
    s = snake(k)
    flat = s
    gsub(/_/, "", flat)
    return s == "webhook_url" || endswith(flat, "password") || endswith(flat, "passphrase") ||
        endswith(flat, "secret") || endswith(flat, "token") || endswith(flat, "key") ||
        endswith(flat, "dsn") || endswith(flat, "passwd") || endswith(flat, "pwd")
}

# learn mode: print a value worth replacing everywhere (6+ characters).
function learned(v) {
    if (learn && length(v) >= 6 && index("[REDACTED]", v) == 0) print v
}

# Split the text after a separator into V_PRE (blanks + opening quote), V_BODY
# (the value), V_POST (closing quote) and V_REST (everything after).
# mode 0: unquoted value ends at a delimiter; 1: at a quote; 2: at end of line.
function take(s, mode,   i, c, q, n, lead, rest) {
    match(s, /^[ \t]*/)
    lead = substr(s, 1, RLENGTH)
    rest = substr(s, RLENGTH + 1)
    V_PRE = lead; V_POST = ""
    q = substr(rest, 1, 2) == "\\\"" ? "\\\"" : substr(rest, 1, 1)
    if (q == "\\\"" || q == "\"" || q == "'") {
        V_PRE = lead q
        rest = substr(rest, length(q) + 1)
        n = length(rest)
        for (i = 1; i <= n; i++) {
            c = substr(rest, i, 1)
            if (c == "\\" && q != "\\\"") { i++; continue }
            if (q == "'" && substr(rest, i, 2) == "''") { i++; continue }
            if (substr(rest, i, length(q)) == q) break
        }
        V_BODY = substr(rest, 1, i - 1)
        if (i <= n) { V_POST = q; V_REST = substr(rest, i + length(q)) }
        else V_REST = ""
        return
    }
    n = length(rest)
    if (mode == 2) i = n + 1
    else {
        for (i = 1; i <= n; i++) {
            c = substr(rest, i, 1)
            if (c == "\"" || c == "'") break
            if (c == "\\" && (substr(rest, i + 1, 1) == "\"" || substr(rest, i + 1, 1) == "'")) break
            if (mode == 0 && index(" \t&", c) > 0) break
        }
    }
    V_BODY = substr(rest, 1, i - 1)
    V_REST = substr(rest, i)
    # b'x', u"x" and SecretStr('x'): the quote opens the value, not ends it.
    if (mode < 2 && i <= n && V_BODY ~ /^([bBuUrRfF]|[A-Za-z]*\()$/) {
        q = V_BODY
        take(substr(rest, i), mode)
        V_PRE = lead q V_PRE
    }
}

function is_blank_value(b) {
    return b == "" || b == "null" || b == "None" || b == "~" || b == "true" || b == "false" ||
        b ~ /^\[REDACTED\]/ || b ~ /\[REDACTED\]$/
}

# KEY=value, "key": "value", key: value. Walks the line key by key so a
# non-secret key never hides a secret one after it.
function redact_keys(s,   out, tok, key, sk, cls, atstart, mode, p) {
    if (index(s, ":") == 0 && index(s, "=") == 0) return s
    out = ""
    while (match(s, KEYRE)) {
        tok = substr(s, RSTART, RLENGTH)
        p = out substr(s, 1, RSTART - 1)
        s = substr(s, RSTART + RLENGTH)
        match(tok, /^[A-Za-z0-9_.-]+/)
        key = substr(tok, 1, RLENGTH)
        out = p tok
        sk = secret_key(key)
        if (snake(key) == "name") {
            take(s, 0)
            pending = secret_key(V_BODY)
            pending_cls = cred_class(V_BODY)
            pending_line = NR
            continue
        }
        cls = cred_class(key)
        if (snake(key) == "value" && pending) { sk = 1; cls = pending_cls; pending = 0 }
        if (!sk) continue
        atstart = (p ~ /^[ \t]*(-[ \t]+)?(export[ \t]+)?$/)
        mode = atstart ? 2 : (snake(key) == "authorization" ? 1 : 0)
        take(s, mode)
        if (is_blank_value(V_BODY) || V_BODY ~ /^[[{]$/ || V_BODY ~ /^\{\{/) continue
        if (substr(s, 1, 2) == "//") continue
        if (V_PRE !~ /["']$/ && V_BODY ~ /^[|>][-+0-9]*$/) {
            # YAML block scalar: the indented lines that follow are the value.
            match(p, /^[ \t]*(-[ \t]+)*/)
            block_indent = RLENGTH
            in_block = 1; block_done = 0
            out = out V_PRE V_BODY
        } else {
            out = out V_PRE "[REDACTED]" V_POST
            n++
            if (cls) learned(V_BODY)
        }
        s = V_REST
    }
    return out s
}

# --name=value and --name value, for a name secret_key() accepts. Only long
# flags: "-p value" is too ambiguous. A value that is itself a flag is left alone.
function redact_flags(s,   out, m, c, sep, ctx, prev) {
    if (index(s, "--") == 0) return s
    out = ""
    while (match(s, FLAGRE)) {
        m = substr(s, RSTART, RLENGTH)
        ctx = out substr(s, 1, RSTART - 1)
        out = ctx m
        s = substr(s, RSTART + RLENGTH)
        prev = substr(ctx, length(ctx), 1)
        c = substr(s, 1, 1)
        if (isword(prev) || prev == "-" || (c != "=" && c != " " && c != "\t")) continue
        if (!secret_key(substr(m, 3))) continue
        sep = c == "=" ? "=" : ""
        take(substr(s, length(sep) + 1), 0)
        if (is_blank_value(V_BODY) || (sep == "" && V_BODY ~ /^-/)) continue
        out = out sep V_PRE "[REDACTED]" V_POST
        n++
        if (cred_class(substr(m, 3))) learned(V_BODY)
        s = V_REST
    }
    return out s
}

# Is the text in ctx glued to a word character on its right edge? "\n" and
# "\t" in a JSON string end a word, they do not extend one.
function word_before(ctx,   prev) {
    prev = substr(ctx, length(ctx), 1)
    if (substr(ctx, length(ctx) - 1, 1) == "\\" && index("ntr", prev)) return 0
    return isword(prev)
}

# Replace each match of re that is at least minlen long and not glued to a
# preceding word character with repl.
function redact_re(s, re, repl, minlen,   out, ctx) {
    out = ""
    while (match(s, re)) {
        ctx = out substr(s, 1, RSTART - 1)
        if (RLENGTH >= minlen && !word_before(ctx)) {
            out = out substr(s, 1, RSTART - 1) repl
            n++
        } else
            out = out substr(s, 1, RSTART + RLENGTH - 1)
        s = substr(s, RSTART + RLENGTH)
    }
    return out s
}

# scheme://user:pass@host keeps scheme, user and host.
function redact_urls(s,   out, m, i, ui, c) {
    if (index(s, "://") == 0) return s
    out = ""
    while (match(s, URLRE)) {
        m = substr(s, RSTART, RLENGTH)
        out = out substr(s, 1, RSTART - 1)
        s = substr(s, RSTART + RLENGTH)
        i = index(m, "://") + 3
        ui = substr(m, i, length(m) - i)
        c = index(ui, ":")
        if (substr(ui, c + 1) != "" && substr(ui, c + 1) != "[REDACTED]") {
            learned(substr(ui, c + 1))
            m = substr(m, 1, i - 1) substr(ui, 1, c) "[REDACTED]@"
            n++
        }
        out = out m
    }
    return out s
}

# -----BEGIN ... PRIVATE KEY----- through the matching END, possibly on one line
# and possibly several times. An unterminated block sets in_pem.
function redact_pem(s,   out) {
    out = ""
    while (match(s, PEM_BEGIN)) {
        out = out substr(s, 1, RSTART - 1) "[REDACTED]"
        n++
        s = substr(s, RSTART + RLENGTH)
        if (!match(s, PEM_END)) { in_pem = 1; return out }
        s = substr(s, RSTART + RLENGTH)
    }
    return out s
}

# First place v stands alone in s: a word character at either edge of v must not
# touch another word character. out is what has already been emitted before s.
function find_exact(s, v, out,   off, p, pos, lw, rw) {
    lw = isword(substr(v, 1, 1))
    rw = isword(substr(v, length(v), 1))
    off = 0
    while ((p = index(substr(s, off + 1), v)) > 0) {
        pos = off + p
        if (!(lw && word_before(substr(out, length(out) - 1) substr(s, 1, pos - 1))) &&
            !(rw && isword(substr(s, pos + length(v), 1))))
            return pos
        off = pos
    }
    return 0
}

# One left-to-right pass over the line, so the replacement text is never scanned
# again; at a tie the longest value (vals is sorted) wins.
function redact_exact(s,   i, out, pos, best, bi) {
    out = ""
    while (s != "") {
        best = 0
        for (i = 1; i <= nvals; i++) {
            pos = find_exact(s, vals[i], out)
            if (pos && (!best || pos < best)) { best = pos; bi = i }
        }
        if (!best) break
        out = out substr(s, 1, best - 1) "[REDACTED]"
        s = substr(s, best + length(vals[bi]))
        n++
    }
    return out s
}

function load_values(path,   line, i, j, tmp, r) {
    while ((r = (getline line < path)) > 0) {
        sub(/\r$/, "", line)
        if (length(line) >= 6 && index("[REDACTED]", line) == 0) vals[++nvals] = line
    }
    if (r < 0) fail("cannot read values file " path)
    close(path)
    # Longest first, so a value that contains another is replaced whole.
    for (i = 2; i <= nvals; i++) {
        tmp = vals[i]
        for (j = i - 1; j >= 1 && length(vals[j]) < length(tmp); j--) vals[j + 1] = vals[j]
        vals[j + 1] = tmp
    }
}

BEGIN {
    PEM_BEGIN = "-----BEGIN [A-Z ]*PRIVATE KEY( BLOCK)?-----"
    PEM_END = "-----END [A-Z ]*PRIVATE KEY( BLOCK)?-----"
    KEYRE = "[A-Za-z_][A-Za-z0-9_.-]*(\\\\)?[\"']?[ \t]*[:=]"
    FLAGRE = "--[A-Za-z][A-Za-z0-9_.-]*"
    URLRE = "[A-Za-z][A-Za-z0-9+.-]*://[^/@ \t\"'?#:]*:[^@ \t\"'?#]*@"
    split("key secret token password passphrase dsn passwd pwd webhook_url", suffix, " ")
    nsuffix = 9
    # Credential-shaped names that are settings, not secrets.
    allow["auth_min_password_length"] = 1
    allow["auth_max_password_bytes"] = 1
    allow["password_reset_ttl_seconds"] = 1
    # Helm values that name a Secret or a key inside it, not a value; support
    # needs to see which Secret an install points at.
    allow["existing_secret"] = 1
    allow["existing_secret_key"] = 1
    allow["existing_secret_password_key"] = 1
    allow["user_password_key"] = 1

    if (names == "") fail("-v names=<secret-names.txt> is required")
    while ((r = (getline line < names)) > 0) {
        sub(/\r$/, "", line)
        gsub(/[_-]/, "", line)
        if (line != "") names_set[tolower(line)] = 1
    }
    if (r < 0) fail("cannot read names file " names)
    close(names)
    if (values != "") load_values(values)
    if (name == "") name = "stdin"
}

failed { next }

{
    line = $0
    if (in_block) {
        match(line, /^[ \t]*/)
        if (line ~ /^[ \t]*$/) { print line; next }
        if (RLENGTH > block_indent) {
            if (!block_done) {
                print substr(line, 1, RLENGTH) "[REDACTED]"
                n++
                block_done = 1
            }
            next
        }
        in_block = 0
    }
    if (in_pem) {
        if (!match(line, PEM_END)) next
        in_pem = 0
        line = substr(line, RSTART + RLENGTH)
        if (line == "") next
        line = redact_pem(line)
    } else
        line = redact_pem(line)
    if (line ~ /^[ \t]*(#.*)?$/) { if (pending) pending_line = NR }
    else if (pending && pending_line < NR - 1) pending = 0
    if (learn && line ~ /^[ \t]*#/) next
    if (learn) redact_urls(line)
    else line = redact_urls(line)
    line = redact_keys(line)
    line = redact_flags(line)
    if (learn) next
    line = redact_re(line, "[Bb][Ee][Aa][Rr][Ee][Rr][ \t]+[A-Za-z0-9._~+/=-]+", "Bearer [REDACTED]", 23)
    line = redact_re(line, "eyJ[A-Za-z0-9_-]+\\.[A-Za-z0-9_-]+\\.[A-Za-z0-9_-]*", "[REDACTED]", 0)
    line = redact_re(line, "sk-[A-Za-z0-9_-]+", "[REDACTED]", 32)
    line = redact_re(line, "xai-[A-Za-z0-9]+", "[REDACTED]", 20)
    line = redact_re(line, "gsk_[A-Za-z0-9]+", "[REDACTED]", 20)
    line = redact_re(line, "glpat-[A-Za-z0-9_-]+", "[REDACTED]", 20)
    line = redact_re(line, "AIza[A-Za-z0-9_-]+", "[REDACTED]", 30)
    line = redact_re(line, "A[KS]IA[A-Z0-9]+", "[REDACTED]", 20)
    line = redact_re(line, "gh[pousr]_[A-Za-z0-9]+", "[REDACTED]", 20)
    line = redact_re(line, "github_pat_[A-Za-z0-9_]+", "[REDACTED]", 30)
    line = redact_re(line, "xox[a-z]-[A-Za-z0-9-]+", "[REDACTED]", 20)
    line = redact_re(line, "xapp-[A-Za-z0-9-]+", "[REDACTED]", 20)
    line = redact_re(line, "[sr]k_(live|test)_[A-Za-z0-9]+", "[REDACTED]", 20)
    line = redact_re(line, "(npm|hf)_[A-Za-z0-9]+", "[REDACTED]", 20)
    line = redact_re(line, "SG\\.[A-Za-z0-9_.-]+", "[REDACTED]", 30)
    line = redact_re(line, "(pdus\\+_|u\\+)[A-Za-z0-9_+-]+", "[REDACTED]", 20)
    if (nvals) line = redact_exact(line)
    print line
}

END {
    if (!failed && counts != "") {
        print (name "\t" (n + 0)) >> counts
        close(counts)
    }
}
