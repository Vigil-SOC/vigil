#!/usr/bin/env bash
# Restore point for the desktop app, source mode. The no-setup repository
# under the upgrade directory is mounted at /var/lib/vigil-upgrade, so both
# commands run in the backend image, as start.sh restore does.
#
#   app_restore.sh --point   print the newest pre-upgrade snapshot as JSON
#   app_restore.sh ID        stop everything holding a database session, then
#                            restore ID. Postgres stays up.
#
# Emits `STEP restore <status>` on stdout for a restore; detail on stderr.
source "$(dirname "$0")/lib.sh"

[ $# -eq 1 ] || { echo "Usage: $0 --point | SNAPSHOT_ID" >&2; exit 1; }
load_env
ensure_docker >&2 || exit 1
prepare_backup_mounts rw
UPGRADE=/var/lib/vigil-upgrade

if [ "$1" = "--point" ]; then
    dc run --rm --no-deps "${BACKUP_RUN_ARGS[@]}" --entrypoint python backup \
        -m core.backup current-point 2>/dev/null
    exit $?
fi

echo "STEP restore start"
# The host API, daemon and worker, plus the backup loop.
"$REPO_ROOT/shutdown_all.sh" >&2
# Bifrost too: the restore replaces its data directory.
dc stop backend soc-daemon llm-worker agent-worker agent-serve bifrost >&2 || true
dc run --rm --no-deps "${BACKUP_RUN_ARGS[@]}" --entrypoint python backup \
    -m core.backup restore --repo "$UPGRADE/repo" \
    --passphrase-file "$UPGRADE/passphrase" --snapshot "$1" \
    --bifrost-data /var/lib/vigil/bifrost >&2 || { echo "STEP restore fail"; exit 1; }
echo "STEP restore ok"
