#!/usr/bin/env bash
# Regenerate requirements.lock from requirements.txt.
#
# requirements.txt is the editable list of direct dependencies with the version
# ranges we accept. requirements.lock is what actually gets installed: every
# package, transitive ones included, pinned to an exact version. Run this after
# changing requirements.txt, and commit both.
#
# The lock is --universal: one file resolved for every platform we support
# (macOS/Linux, arm64/x86_64), with environment markers where a package differs
# between them. Do not regenerate it with a plain `uv pip compile` -- that
# resolves for the machine you happen to be on and silently drops the others.
source "$(dirname "$0")/lib.sh"

ensure_uv || exit 1

# uv copies its arguments into the lock header, so relative paths keep the
# committer's home directory out of the file.
cd "$REPO_ROOT" || exit 1

echo "Resolving dependencies for Python $(python_pin) across all platforms..."
"$UV" pip compile requirements.txt \
    --universal \
    --python-version "$(python_pin)" \
    -o requirements.lock || {
    echo "Resolution failed. requirements.lock is unchanged." >&2
    exit 1
}

echo ""
echo "Wrote requirements.lock ($(grep -cvE '^\s*(#|$)' "$REPO_ROOT/requirements.lock") entries)."
echo "Review the diff, then commit requirements.txt and requirements.lock together."
