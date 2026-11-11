#!/usr/bin/env bash
# EXPERIMENTAL: this script is experimental and has only been tested on
# Debian 13 and compatible Linux builds. Other distributions may require
# modification.
#
# Vigil pre-change backup for a start.sh / systemd install.
#
# Layers, from most to least authoritative:
#   1. Native snapshot   ./start.sh backup  (encrypted restic repo: Postgres
#      pg_dump -Fc under an exported snapshot, ~/.vigil incl. master.key and
#      secrets.enc, .env, investigations, Bifrost). Proven restorable with
#      `restore --test` before this script reports success.
#   2. Independent dump  pg_dump -Fc via the Postgres container, checked with
#      pg_restore --list. Restorable without any Vigil code.
#   3. Extras bundle     what a snapshot of data does not hold: uncommitted
#      local code changes (as a patch), data/, the systemd unit, image list.
#      Encrypted with the same passphrase (openssl).
#
# Online: nothing is stopped. Safe to run from cron.
#
# Usage: vigil-backup.sh [--dry-run] [--no-verify] [--keep N] [--offsite DEST]
#   --dry-run     preflight only; writes nothing
#   --no-verify   skip the `restore --test` pass (not recommended)
#   --keep N      keep the newest N dumps/bundles (default 14)
#   --offsite D   after the run, copy this run's files to D (user@host:/path)
#
# Env: VIGIL_DIR (~/vigil)  BACKUP_ROOT (~/vigil-backups)  PG_CONTAINER
#      (deeptempo-postgres)  PG_USER (deeptempo)  PG_DB (deeptempo_soc)
#
# Writes $BACKUP_ROOT/last-backup.env, which vigil-upgrade.sh reads.
set -Eeuo pipefail
umask 077

main() {
    local dry=0 verify=1 keep=14 offsite=""
    while [ $# -gt 0 ]; do
        case "$1" in
            --dry-run) dry=1 ;;
            --no-verify) verify=0 ;;
            --keep) keep="${2:?--keep needs a number}"; shift ;;
            --offsite) offsite="${2:?--offsite needs a destination}"; shift ;;
            -h|--help) sed -n '2,28p' "${BASH_SOURCE[0]:-$0}"; return 0 ;;
            *) echo "unknown argument: $1" >&2; return 1 ;;
        esac
        shift
    done

    # Run as the service user: it owns the repo, ~/.vigil and the docker group.
    if [ "$(id -u)" -eq 0 ]; then
        [ -f "${BASH_SOURCE[0]}" ] || die "run as the vigil user when piping the script"
        exec runuser -u vigil -- "${BASH_SOURCE[0]}" "$@"
    fi

    VIGIL_DIR="${VIGIL_DIR:-$HOME/vigil}"
    BACKUP_ROOT="${BACKUP_ROOT:-$HOME/vigil-backups}"
    PG_CONTAINER="${PG_CONTAINER:-deeptempo-postgres}"
    PG_USER="${PG_USER:-deeptempo}"
    PG_DB="${PG_DB:-deeptempo_soc}"
    REPO="$BACKUP_ROOT/restic"
    PASSFILE="$BACKUP_ROOT/passphrase"
    local ts; ts="$(date -u +%Y%m%dT%H%M%SZ)"
    local run_dir="$BACKUP_ROOT/runs/$ts"
    local dump="$run_dir/vigil-$ts.pgdump"
    local extras="$run_dir/vigil-extras-$ts.tar.enc"

    # ---- preflight ----
    log "preflight"
    [ -x "$VIGIL_DIR/start.sh" ] || die "no start.sh in $VIGIL_DIR"
    docker info >/dev/null 2>&1 || die "docker is not reachable by $(id -un)"
    [ "$(docker inspect -f '{{.State.Health.Status}}' "$PG_CONTAINER" 2>/dev/null)" = healthy ] \
        || die "$PG_CONTAINER is not healthy; a backup of a sick database proves nothing"
    local version rev
    version="$(cat "$VIGIL_DIR/VERSION")"
    rev="$(git -C "$VIGIL_DIR" rev-parse --short HEAD)"
    local db_bytes free_kb need_kb
    db_bytes="$(psql_q "select pg_database_size('$PG_DB')")"
    mkdir -p "$BACKUP_ROOT"
    free_kb="$(df -Pk "$BACKUP_ROOT" | awk 'NR==2{print $4}')"
    need_kb=$(( (db_bytes / 1024) * 3 + 2 * 1024 * 1024 ))   # snapshot + dump + headroom
    [ "$free_kb" -gt "$need_kb" ] \
        || die "need ~$((need_kb/1024)) MiB free under $BACKUP_ROOT, have $((free_kb/1024)) MiB"
    log "version $version ($rev), db $((db_bytes/1048576)) MiB, $((free_kb/1024)) MiB free"

    if [ "$dry" -eq 1 ]; then
        log "dry run: would write $run_dir and a snapshot in $REPO"
        [ -f "$PASSFILE" ] || log "dry run: would create passphrase $PASSFILE"
        return 0
    fi

    exec 9>"$BACKUP_ROOT/.lock"
    flock -n 9 || die "another backup is running"
    mkdir -p "$run_dir" "$REPO"
    chmod 700 "$BACKUP_ROOT" "$run_dir"
    if [ ! -f "$PASSFILE" ]; then
        head -c 32 /dev/urandom | base64 > "$PASSFILE"
        chmod 600 "$PASSFILE"
        warn "created $PASSFILE. COPY IT OFF THIS HOST NOW: without it the snapshot and extras are unreadable."
    fi

    # ---- 1. native snapshot ----
    # start.sh runs the snapshot inside vigil-backend:local. A failed or rolled-back
    # upgrade leaves the other release's image under that tag, and its CLI differs
    # (0.9.0's pre-upgrade needs VIGIL_UPGRADE_DIR), so build it from this checkout.
    log "building vigil-backend:local from $version"
    (cd "$VIGIL_DIR" && bash -c 'source scripts/lib.sh && dc build backup-pre-upgrade') \
        >"$run_dir/build.log" 2>&1 \
        || { tail -20 "$run_dir/build.log" >&2; die "could not build vigil-backend:local"; }
    log "native snapshot"
    local out snap
    out="$(cd "$VIGIL_DIR" && ./start.sh backup --repo "$REPO" --passphrase-file "$PASSFILE" 2>"$run_dir/snapshot.log")" \
        || { tail -20 "$run_dir/snapshot.log" >&2; die "native snapshot failed"; }
    snap="$(printf '%s\n' "$out" | grep -E '^[0-9a-f]{8,64}$' | tail -1 || true)"
    [ -n "$snap" ] || die "snapshot finished but printed no snapshot id: $out"
    log "snapshot $snap"

    # ---- 2. independent logical dump ----
    log "independent pg_dump"
    docker exec "$PG_CONTAINER" pg_dump -U "$PG_USER" -d "$PG_DB" -Fc > "$dump"
    local entries
    entries="$(docker exec -i "$PG_CONTAINER" pg_restore --list < "$dump" | grep -c . || true)"
    [ "$entries" -gt 50 ] || die "dump looks empty ($entries TOC entries)"
    log "dump ok: $((  $(stat -c %s "$dump") / 1048576 )) MiB, $entries TOC entries"

    # ---- 3. extras bundle ----
    log "extras bundle"
    local stage; stage="$(mktemp -d "$run_dir/stage.XXXXXX")"
    (
        cd "$VIGIL_DIR"
        git diff HEAD > "$stage/local-changes.patch" || true
        git status --porcelain > "$stage/git-status.txt"
        git rev-parse HEAD > "$stage/git-head.txt"
        # Untracked files that matter (not ignored ones such as venv).
        git ls-files --others --exclude-standard -z | tar --null -T - -cf "$stage/untracked.tar" 2>/dev/null || true
        tar -cf "$stage/data.tar" data 2>/dev/null || true
        cp /etc/systemd/system/vigil.service "$stage/" 2>/dev/null || true
        docker images --format '{{.Repository}}:{{.Tag}} {{.ID}}' > "$stage/images.txt"
        cp VERSION "$stage/VERSION"
    )
    tar -C "$stage" -cf - . | openssl enc -aes-256-cbc -pbkdf2 -salt -pass "file:$PASSFILE" -out "$extras"
    rm -rf "$stage"

    # ---- verify ----
    if [ "$verify" -eq 1 ]; then
        log "verifying: restore --test of snapshot $snap (live state untouched)"
        (cd "$VIGIL_DIR" && ./start.sh restore --repo "$REPO" --passphrase-file "$PASSFILE" \
            --snapshot "$snap" --test) >"$run_dir/verify.log" 2>&1 \
            || { tail -20 "$run_dir/verify.log" >&2; die "snapshot did not pass restore --test"; }
        openssl enc -d -aes-256-cbc -pbkdf2 -pass "file:$PASSFILE" -in "$extras" | tar -tf - >/dev/null \
            || die "extras bundle does not decrypt"
        log "verified"
    fi

    # ---- manifest ----
    psql_q "
      select 'findings='||count(*) from findings union all
      select 'cases='||count(*) from cases union all
      select 'case_findings='||count(*) from case_findings union all
      select 'approval_actions='||count(*) from approval_actions union all
      select 'users='||count(*) from users union all
      select 'workflow_runs='||count(*) from workflow_runs" > "$run_dir/counts.txt"
    ( cd "$run_dir" && sha256sum "$(basename "$dump")" "$(basename "$extras")" > SHA256SUMS )
    {
        echo "TIMESTAMP=$ts"
        echo "VERSION=$version"
        echo "GIT_REV=$rev"
        echo "SNAPSHOT_ID=$snap"
        echo "REPO=$REPO"
        echo "PASSFILE=$PASSFILE"
        echo "RUN_DIR=$run_dir"
        echo "DUMP=$dump"
        echo "EXTRAS=$extras"
    } > "$BACKUP_ROOT/last-backup.env"

    # ---- retention (dumps/bundles; the restic repo is managed by core.backup) ----
    ls -1dt "$BACKUP_ROOT"/runs/*/ 2>/dev/null | tail -n +$((keep + 1)) | xargs -r rm -rf --

    # ---- off-host copy ----
    if [ -n "$offsite" ]; then
        log "copying to $offsite"
        # The passphrase is deliberately not copied: keep it somewhere else.
        if command -v rsync >/dev/null; then
            rsync -a "$run_dir" "$offsite/runs/"
            rsync -a "$REPO/" "$offsite/restic/"
        else
            scp -rq "$run_dir" "$REPO" "$offsite/"
        fi
    else
        warn "no --offsite given: these backups share a disk with the system they protect"
    fi

    log "DONE  snapshot=$snap  dir=$run_dir"
    echo "restore the snapshot:  cd $VIGIL_DIR && ./start.sh restore --repo $REPO --passphrase-file $PASSFILE --snapshot $snap"
}

psql_q() { docker exec "$PG_CONTAINER" psql -U "$PG_USER" -d "$PG_DB" -Atc "$1"; }
log()  { printf '%s [backup] %s\n' "$(date -u +%T)" "$*"; }
warn() { printf '%s [backup] WARNING: %s\n' "$(date -u +%T)" "$*" >&2; }
die()  { printf '%s [backup] ERROR: %s\n' "$(date -u +%T)" "$*" >&2; exit 1; }

main "$@"
exit
