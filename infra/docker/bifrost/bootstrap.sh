#!/bin/sh
# Bifrost entrypoint wrapper (#2210). Writes the config Bifrost reads with
# every provider key dropped whose "env.X" references an unset or empty
# variable, then hands over to the image's own entrypoint.
#
# Bifrost v2.2.1 creates every config.json key on start, credential or not, and
# re-adds any its store is missing on every restart. An unset env var therefore
# became an empty key that took a weighted share of requests and failed them.
# With the var set the key is seeded as before (#1049). Provider blocks are kept
# even when all their keys go, so their network_config still applies.
#
# The image is Alpine with sh/awk, no jq, so this walks the JSON in awk. It only
# rewrites the arrays under a "keys" member; everything else is copied verbatim.
set -e

src=/app/bootstrap/config.json
dst=/app/data/config.json

awk '
function str_end(i,   c) {   # index of the quote closing the string opening at i
  for (i++; i <= n; i++) {
    c = substr(buf, i, 1)
    if (c == "\\") i++
    else if (c == "\"") return i
  }
  return n
}
function obj_end(i,   c, d) {   # index of the brace closing the object at i
  for (d = 0; i <= n; i++) {
    c = substr(buf, i, 1)
    if (c == "\"") i = str_end(i)
    else if (c == "{") d++
    else if (c == "}" && --d == 0) return i
  }
  return n
}
function keep(obj,   rest, name) {   # false when any env.X it names is unset
  rest = obj
  while (match(rest, /"env\.[A-Za-z_][A-Za-z0-9_]*"/)) {
    name = substr(rest, RSTART + 5, RLENGTH - 6)
    if (ENVIRON[name] == "") return 0
    rest = substr(rest, RSTART + RLENGTH)
  }
  return 1
}
function keys_array(i,   c, e, kept, sep) {   # i is just past "["; returns past "]"
  kept = ""; sep = ""
  for (; i <= n; i++) {
    c = substr(buf, i, 1)
    if (c == "]") break
    if (c != "{") continue   # whitespace and commas between elements
    e = obj_end(i)
    if (keep(substr(buf, i, e - i + 1))) { kept = kept sep substr(buf, i, e - i + 1); sep = ", " }
    i = e
  }
  out = out kept "]"
  return i + 1
}
{ buf = buf $0 "\n" }
END {
  n = length(buf); out = ""; want = 0
  for (i = 1; i <= n; ) {
    c = substr(buf, i, 1)
    if (c == "\"") {
      e = str_end(i); tok = substr(buf, i, e - i + 1); out = out tok; i = e + 1
      want = (tok == "\"keys\"")
      continue
    }
    out = out c; i++
    if (c == "[" && want) { i = keys_array(i); want = 0 }
    else if (c !~ /[ \t\r\n:]/) want = 0
  }
  printf "%s", out
}' "$src" > "$dst.tmp"
# Rename, not overwrite: a volume that predates this holds a root-owned
# config.json left behind by the old read-only bind mount.
mv -f "$dst.tmp" "$dst"

exec /app/docker-entrypoint.sh "$@"
