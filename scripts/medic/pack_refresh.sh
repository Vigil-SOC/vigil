#!/usr/bin/env bash
# scripts/medic/pack_refresh.sh — refresh medic-core's expiry (medic-pack-refresh.yml).
#
#   scripts/medic/pack_refresh.sh [bundled_file]
#
# Run from the repo root on main. Does nothing (exit 0) unless the bundled pack
# (default services/medic/packs/bundled/medic-core.json) expires within 75 days AND
# pack.yaml hasn't already been given a later expires_at (a refresh or release is in
# flight). Otherwise edits only services/medic/packs/medic-core/pack.yaml: PATCH
# version up one, created_at = now, expires_at = now + 180 days (under pack_check's
# 366-day cap; pack_diff classes the change as PATCH). Targeted text replaces, so the
# comments and key order survive. Writes refresh=true and version=X.Y.Z to $GITHUB_OUTPUT
# (stdout without it) when it edited. NOW (ISO 8601, e.g. 2027-01-01T00:00:00Z) overrides the clock.
set -euo pipefail

exec python3 -I - "${1:-services/medic/packs/bundled/medic-core.json}" "${NOW:-}" <<'PY'
import json
import os
import re
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

bundled, now = Path(sys.argv[1]), sys.argv[2]
now = datetime.fromisoformat(now).astimezone(UTC) if now else datetime.now(UTC)
now = now.replace(microsecond=0)
pack = Path("services/medic/packs/medic-core/pack.yaml")
fmt = "%Y-%m-%dT%H:%M:%SZ"

if not bundled.is_file():
    print(f"::notice::{bundled} is missing; nothing to refresh.")
    sys.exit(0)
have = datetime.strptime(
    json.loads(bundled.read_text())["manifest"]["pack"]["expires_at"], fmt
).replace(tzinfo=UTC)
left = (have - now).days
if have - now > timedelta(days=75):
    print(f"::notice::Bundled pack expires {have:{fmt}}, {left} days away; nothing to do.")
    sys.exit(0)

text = pack.read_text()
field = lambda k: re.search(rf'^  {k}: "?([^"\n]+)"?$', text, re.M)
cur = datetime.strptime(field("expires_at").group(1), fmt).replace(tzinfo=UTC)
if cur > have:
    print(f"::notice::pack.yaml already expires {cur:{fmt}}, later than the bundled {have:{fmt}}; a refresh is in flight.")
    sys.exit(0)

major, minor, patch = field("version").group(1).split(".")
version = f"{major}.{minor}.{int(patch) + 1}"
for key, val, quote in (
    ("version", version, ""),
    ("created_at", f"{now:{fmt}}", '"'),
    ("expires_at", f"{now + timedelta(days=180):{fmt}}", '"'),
):
    text, n = re.subn(rf"^(  {key}: ).*$", lambda m: f"{m[1]}{quote}{val}{quote}", text, count=1, flags=re.M)
    assert n == 1, f"no {key} in {pack}"
pack.write_text(text)
print(f"::notice::Bundled pack expires {have:{fmt}} ({left} days); pack.yaml now {version}, expires {now + timedelta(days=180):{fmt}}.")
with open(os.environ.get("GITHUB_OUTPUT", "/dev/stdout"), "a") as out:
    out.write(f"refresh=true\nversion={version}\n")
PY
