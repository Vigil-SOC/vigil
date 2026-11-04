#!/usr/bin/env bash
# scripts/medic/check_bundled_fresh.sh — the bundled-pack ratchet (medic-bundled-ratchet.yml).
#
#   scripts/medic/check_bundled_fresh.sh [bundled_file]
#
# Run from the repo root. Fails unless the bundled pack (default
# services/medic/packs/bundled/medic-core.json) passes the importer's own pack_check
# for the Vigil version in ./VERSION and still has at least 60 days to run. So a
# release can't ship a pack whose Vigil range misses VERSION (P-VIGIL) or that is
# about to expire (P-EXPIRED). NOW (ISO 8601, e.g. 2027-01-01T00:00:00Z) overrides
# the clock; medic-pack-refresh.yml opens a refresh long before this bites.
# MISSING_OK=1 turns a missing file into a notice: the workflow sets it for a PR that
# does not change VERSION (a change to this check itself), where no release is at stake.
set -euo pipefail

file=${1:-services/medic/packs/bundled/medic-core.json}
if [ ! -f "$file" ] && [ "${MISSING_OK:-}" = 1 ]; then
  echo "::notice::$file is missing, but this change does not touch VERSION, so no release ships without it."
  exit 0
fi
if [ ! -f "$file" ]; then
  echo "::error::$file is missing; a release without a bundled pack is the defect this check guards against. Merge the medic-core bundled-file PR first."
  exit 1
fi

uv run --project services/medic --frozen python - "$file" "$(cat VERSION)" "${NOW:-}" <<'PY'
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

from services.medic.contracts.pack_check import check_pack

path, version, now = sys.argv[1], sys.argv[2], sys.argv[3]
now = datetime.fromisoformat(now) if now else datetime.now(UTC)
# 60 days ahead: P-EXPIRED here means the pack expires inside that window.
res = check_pack(
    Path(path).read_bytes(), vigil_version=version, now=now + timedelta(days=60)
)
m = res.manifest or {}
expires, vigil = m.get("pack", {}).get("expires_at"), m.get("vigil", {})
rng = f"[{vigil.get('min')}, {vigil.get('max_exclusive')})"
for code, msg in res.errors:
    if code == "P-VIGIL":
        print(f"::error::{path}: Vigil range {rng} misses VERSION {version}; rebuild the pack for this release ({msg})")
    elif code == "P-EXPIRED":
        print(f"::error::{path}: expires_at {expires} is within 60 days of {now:%Y-%m-%d}; refresh the pack before releasing (Vigil range {rng})")
    else:
        print(f"::error::{path}: pack_check {code}: {msg}")
if res.ok:
    print(f"{path} fits Vigil {version} {rng} and expires {expires}, more than 60 days from {now:%Y-%m-%d}.")
sys.exit(0 if res.ok else 1)
PY
