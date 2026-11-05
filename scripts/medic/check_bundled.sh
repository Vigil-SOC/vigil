#!/usr/bin/env bash
# scripts/medic/check_bundled.sh — the bundled-file check (medic-bundled-check.yml).
#
#   <changed paths, one per line> | scripts/medic/check_bundled.sh
#
# Run from the repo root with `gh` authenticated and GH_REPO set. Passes at once when
# nothing under services/medic/packs/bundled/ changed. Otherwise fails unless
# medic-core.json is the only file changed there and its SHA-256 equals the hash on
# the Release for the tag its own manifest.pack.version names.
set -euo pipefail

dir=services/medic/packs/bundled
file=$dir/medic-core.json

# Paths here (both sides of a rename) other than the one allowed file.
touched=$(grep "^$dir/" || true)
if [ -z "$touched" ]; then
  echo "::notice::No change under $dir/; nothing to check."
  exit 0
fi
extra=$(grep -vxF "$file" <<<"$touched" || true)
if [ -n "$extra" ]; then
  echo "::error::$file must be the only file changed under $dir/; also changed:"
  echo "$extra"
  exit 1
fi
[ -f "$file" ] || { echo "::error::$file is missing"; exit 1; }

# Parse as data and take the version from the file itself, never from the PR.
version=$(jq -er '.manifest.pack.version' "$file") \
  || { echo "::error::$file has no manifest.pack.version"; exit 1; }
[[ "$version" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] \
  || { echo "::error::manifest.pack.version '$version' is not X.Y.Z"; exit 1; }
tag=medic-core-v$version

# The hash is the first line of the Release body: "SHA-256: `<hex>`".
body=$(gh release view "$tag" --json body --jq .body) \
  || { echo "::error::no Release for $tag; the file must come from a medic-core release"; exit 1; }
expected=$(head -n1 <<<"$body" | sed -nE 's/^SHA-256: `([0-9a-f]{64})`.*/\1/p')
[ -n "$expected" ] || { echo "::error::Release $tag has no SHA-256 on its first line"; exit 1; }

actual=$(sha256sum "$file" | cut -d' ' -f1)
if [ "$actual" != "$expected" ]; then
  echo "::error::$file is $actual; Release $tag says $expected"
  exit 1
fi
echo "$file matches the $tag Release ($actual)."
