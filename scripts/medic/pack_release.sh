#!/usr/bin/env bash
# scripts/medic/pack_release.sh — build, check and describe a medic-core release.
#
#   scripts/medic/pack_release.sh <out_dir> <base_ref>
#
# Run from the repo root; medic-pack-release.yml calls exactly this. <base_ref> is
# the last medic-core-v* tag, or before the first tag the commit to diff against.
# 1. Builds services/medic/packs/medic-core twice and fails if the bytes differ.
# 2. Runs pack_check on the built file (the Vigil version is ./VERSION).
# 3. Runs pack_diff against <base_ref>; it fails when pack.yaml's version is below
#    the bump the change needs.
# Writes to <out_dir>: medic-core.json, medic-core.json.sha256, pack-diff.md and
# release-notes.md (the Release body: the hash first, then the diff and the commits).
set -euo pipefail

out=${1:?usage: pack_release.sh <out_dir> <base_ref>}
base=${2:?usage: pack_release.sh <out_dir> <base_ref>}
pack=services/medic/packs/medic-core
run() { uv run --project services/medic --frozen "$@"; }

mkdir -p "$out"
run python services/medic/contracts/pack_build.py "$pack" "$out/medic-core.json"
run python services/medic/contracts/pack_build.py "$pack" "$out/second-build.json" >/dev/null
cmp "$out/medic-core.json" "$out/second-build.json" \
  || { echo "::error::two builds of $pack differ; the build is not reproducible"; exit 1; }
rm "$out/second-build.json"
echo "two builds are byte-identical"

sum=$(sha256sum "$out/medic-core.json" | cut -d' ' -f1)
echo "$sum  medic-core.json" >"$out/medic-core.json.sha256"

# pack_check is the importer's own check, on the bytes that will ship.
run python - "$out/medic-core.json" "$(cat VERSION)" <<'EOF'
import sys
from datetime import UTC, datetime
from pathlib import Path

from services.medic.contracts.pack_check import check_pack

res = check_pack(
    Path(sys.argv[1]).read_bytes(),
    vigil_version=sys.argv[2],
    now=datetime.now(UTC),
)
for code, msg in res.errors:
    print(f"::error::pack_check {code}: {msg}")
sys.exit(0 if res.ok else 1)
EOF
echo "pack_check passed on $out/medic-core.json"

# The baseline: the same `git archive` pattern as medic.yml's Pack diff step.
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
git archive "$base" "$pack" | tar -x -C "$tmp"
set +e
run python -m services.medic.packs.diff "$tmp/$pack" "$pack" >"$out/pack-diff.md"
code=$?
set -e
cat "$out/pack-diff.md"

{
  echo "SHA-256: \`$sum\`"
  echo
  echo "Asset: \`medic-core.json\` (hash also in \`medic-core.json.sha256\`). Built from \`$(git rev-parse --short HEAD)\` with \`pack_build.py\`; rebuild it from that commit to compare."
  echo
  echo "Baseline: \`$base\`"
  echo
  echo "## Pack diff"
  echo
  cat "$out/pack-diff.md"
  echo
  echo "## Changelog"
  echo
  git log --no-merges --format='- %s (%h)' "$base..HEAD" -- "$pack"
} >"$out/release-notes.md"

exit "$code"
