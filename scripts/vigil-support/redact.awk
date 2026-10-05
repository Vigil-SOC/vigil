#!/usr/bin/awk -f
# Support-bundle redaction filter: stdin to stdout, POSIX awk (gawk, mawk, BSD).
#
#   awk -f redact.awk -v names=secret-names.txt [-v values=exact.txt] \
#       [-v counts=counts.tsv -v name=<label>] < in > out
#
# names   one secret env-var name per line (required; the filter fails closed
#         without it). Matched case-insensitively with "_" and "-" ignored, so
#         POSTGRES_PASSWORD, postgresPassword and postgres-password all hit.
# values  optional file of exact values learned elsewhere, one per line. Each of
#         6+ characters is replaced wherever it appears (fixed-string match).
# counts  optional side file; "<name><TAB><replacements>" is appended at the end.
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
        endswith(flat, "apikey") || endswith(flat, "token") || s == "dsn") return 1
    for (i = 1; i <= nsuffix; i++)
        if (endswith(s, "_" suffix[i])) return 1
    return 0
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
            if (mode == 0 && index(" \t,;&)]}", c) > 0) break
        }
    }
    V_BODY = substr(rest, 1, i - 1)
    V_REST = substr(rest, i)
}

function is_blank_value(b) {
    return b == "" || b == "[REDACTED]" || b == "null" || b == "None" || b == "~" || b == "true" || b == "false"
}

# KEY=value, "key": "value", key: value. Walks the line key by key so a
# non-secret key never hides a secret one after it.
function redact_keys(s,   out, tok, key, sk, atstart, mode, p) {
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
            pending_line = NR
            continue
        }
        if (snake(key) == "value" && pending) { sk = 1; pending = 0 }
        if (!sk) continue
        atstart = (p ~ /^[ \t]*(-[ \t]+)?(export[ \t]+)?$/)
        mode = atstart ? 2 : (snake(key) == "authorization" ? 1 : 0)
        take(s, mode)
        if (is_blank_value(V_BODY) || V_BODY ~ /^[[{]/) continue
        if (V_PRE !~ /["']$/ && V_BODY ~ /^[|>][-+0-9]*$/) {
            # YAML block scalar: the indented lines that follow are the value.
            match(p, /^[ \t]*/)
            block_indent = RLENGTH
            in_block = 1; block_done = 0
            out = out V_PRE V_BODY
        } else {
            out = out V_PRE "[REDACTED]" V_POST
            n++
        }
        s = V_REST
    }
    return out s
}

# Replace each match of re that is at least minlen long and not glued to a
# preceding word character with repl.
function redact_re(s, re, repl, minlen,   out, prev) {
    out = ""
    while (match(s, re)) {
        prev = RSTART > 1 ? substr(s, RSTART - 1, 1) : substr(out, length(out), 1)
        if (RLENGTH >= minlen && !isword(prev)) {
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
    out = ""
    while (match(s, URLRE)) {
        m = substr(s, RSTART, RLENGTH)
        out = out substr(s, 1, RSTART - 1)
        s = substr(s, RSTART + RLENGTH)
        i = index(m, "://") + 3
        ui = substr(m, i, length(m) - i)
        c = index(ui, ":")
        if (c > 0 && substr(ui, c + 1) != "" && substr(ui, c + 1) != "[REDACTED]") {
            m = substr(m, 1, i - 1) substr(ui, 1, c) "[REDACTED]@"
            n++
        }
        out = out m
    }
    return out s
}

# -----BEGIN ... PRIVATE KEY----- through the matching END line, possibly on one line.
function redact_pem(s,   b, e, rest, after) {
    b = index(s, "-----BEGIN ")
    if (b == 0) return s
    rest = substr(s, b)
    if (rest !~ /^-----BEGIN [A-Z ]*PRIVATE KEY-----/) return s
    n++
    if (match(rest, /-----END [A-Z ]*PRIVATE KEY-----/))
        return substr(s, 1, b - 1) "[REDACTED]" substr(rest, RSTART + RLENGTH)
    in_pem = 1
    return substr(s, 1, b - 1) "[REDACTED]"
}

function redact_exact(s,   i, v, out, p) {
    for (i = 1; i <= nvals; i++) {
        v = vals[i]
        out = ""
        while ((p = index(s, v)) > 0) {
            out = out substr(s, 1, p - 1) "[REDACTED]"
            s = substr(s, p + length(v))
            n++
        }
        s = out s
    }
    return s
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
    KEYRE = "[A-Za-z_][A-Za-z0-9_.-]*(\\\\)?[\"']?[ \t]*[:=]"
    URLRE = "[A-Za-z][A-Za-z0-9+.-]*://[^/@ \t\"']*@"
    split("key secret token password passphrase dsn", suffix, " ")
    nsuffix = 6
    # Credential-shaped names that are settings, not secrets.
    allow["auth_min_password_length"] = 1
    allow["auth_max_password_bytes"] = 1
    allow["password_reset_ttl_seconds"] = 1

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
        if (match(line, /-----END [A-Z ]*PRIVATE KEY-----/)) {
            in_pem = 0
            if (RSTART + RLENGTH <= length(line)) print substr(line, RSTART + RLENGTH)
        }
        next
    }
    if (pending && pending_line < NR - 1) pending = 0
    line = redact_pem(line)
    if (in_pem) { print line; next }
    line = redact_keys(line)
    line = redact_urls(line)
    line = redact_re(line, "[Bb][Ee][Aa][Rr][Ee][Rr][ \t]+[A-Za-z0-9._~+/=-]+", "Bearer [REDACTED]", 15)
    line = redact_re(line, "eyJ[A-Za-z0-9_-]+\\.[A-Za-z0-9_-]+\\.[A-Za-z0-9_-]*", "[REDACTED]", 0)
    line = redact_re(line, "sk-[A-Za-z0-9_-]+", "[REDACTED]", 20)
    line = redact_re(line, "xai-[A-Za-z0-9]+", "[REDACTED]", 20)
    line = redact_re(line, "gsk_[A-Za-z0-9]+", "[REDACTED]", 20)
    line = redact_re(line, "glpat-[A-Za-z0-9_-]+", "[REDACTED]", 20)
    line = redact_re(line, "AIza[A-Za-z0-9_-]+", "[REDACTED]", 30)
    line = redact_re(line, "A[KS]IA[A-Z0-9]+", "[REDACTED]", 20)
    line = redact_re(line, "gh[pousr]_[A-Za-z0-9]+", "[REDACTED]", 20)
    line = redact_re(line, "github_pat_[A-Za-z0-9_]+", "[REDACTED]", 30)
    line = redact_re(line, "xox[abprs]-[A-Za-z0-9-]+", "[REDACTED]", 20)
    line = redact_re(line, "xapp-[A-Za-z0-9-]+", "[REDACTED]", 20)
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
