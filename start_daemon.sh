#!/bin/bash
# Deprecated shim — use start.sh --daemon instead.
# This script will be removed in a future release.
exec "$(dirname "$0")/start.sh" --daemon "$@"
