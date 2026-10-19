#!/usr/bin/env bash
# Checks out the engine the kernel replaced, at the commit this branch forked from,
# as a worktree beside this one, and points it at this checkout's dependencies
# (services/agent/package.json is identical at both). Idempotent.
#
#   eval/setup-baseline.sh [dir]     default: ../vigil-eval-baseline next to the repo
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(git -C "$HERE" rev-parse --show-toplevel)"
BASE="44e03ffa825ee5422a95ea2d2b803e8f4176d5f5"
DIR="${1:-$(dirname "$REPO")/vigil-eval-baseline}"
if [ ! -d "$DIR" ]; then
  git -C "$REPO" worktree add --detach "$DIR" "$BASE"
fi
if ! diff -q "$DIR/services/agent/package.json" "$REPO/services/agent/package.json" >/dev/null; then
  echo "package.json differs between the baseline and this checkout; install the baseline's own dependencies instead" >&2
  exit 1
fi
ln -sfn "$REPO/services/agent/node_modules" "$DIR/services/agent/node_modules"
echo "$DIR"
