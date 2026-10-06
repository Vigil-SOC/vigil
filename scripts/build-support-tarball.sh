#!/bin/sh
# Build the vigil-support release asset: vigil-support-<version>.tar.gz and its
# .sha256, in <outdir>. The release workflow and the tests run this same script.
# Usage: scripts/build-support-tarball.sh <version> <outdir>   (version has no leading "v")
set -eu

[ $# -eq 2 ] || { echo "usage: $0 <version> <outdir>" >&2; exit 2; }
VERSION=$1
case $VERSION in '' | v* | *[!0-9A-Za-z.+_-]*) echo "$0: bad version '$VERSION'" >&2; exit 2 ;; esac

SRC=$(cd "$(dirname "$0")/vigil-support" && pwd)
mkdir -p "$2"
OUT=$(cd "$2" && pwd)
NAME=vigil-support-$VERSION.tar.gz

STAGE=$(mktemp -d)
trap 'rm -rf "$STAGE"' EXIT

cp -pR "$SRC" "$STAGE/vigil-support"
# Stamp the tag's version over the committed one (which carries a release-please marker).
echo "$VERSION" >"$STAGE/vigil-support/VERSION"

tar -czf "$OUT/$NAME" -C "$STAGE" vigil-support

# Run from OUT so the .sha256 holds the bare file name and `-c` works as pasted.
cd "$OUT"
if command -v sha256sum >/dev/null 2>&1; then
    sha256sum "$NAME" >"$NAME.sha256"
else
    shasum -a 256 "$NAME" >"$NAME.sha256"
fi
echo "built $OUT/$NAME"
