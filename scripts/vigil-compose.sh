#!/usr/bin/env bash
# EXPERIMENTAL: this script is experimental and has only been tested on
# Debian 13 and compatible Linux builds. Other distributions may require
# modification.
#
# Vigil backup / restore for a Docker Compose deployment
# (infra/docker/docker-compose.yml, state in named volumes).
#
#   vigil-compose.sh backup  [--no-verify] [--keep N] [--offsite DEST] [--dry-run]
#   vigil-compose.sh restore [--snapshot ID|latest] [--test] [--yes] [--clear-env-jwt]
#   vigil-compose.sh list
#
# BACKUP is online: nothing is stopped. It uses the stack's own `backup` service
# (python -m core.backup in the vigil-backend image: pg_dump -Fc under an
# exported snapshot, vigil_home incl. master.key/secrets.enc, investigations,
# Bifrost) writing an encrypted restic repo to a HOST directory, then adds
#   - an independent pg_dump -Fc (restorable with plain pg_restore), and
#   - an encrypted extras bundle: the compose .env (POSTGRES_PASSWORD, JWT...),
#     local code diff, image and volume lists,
# and proves the snapshot with `restore --test` before reporting success.
#
# RESTORE needs the stack quiet: everything holding a DB session or Bifrost's
# files is stopped (Postgres and Redis stay up), a safety pg_dump of the CURRENT
# database is taken, the snapshot is swapped in, the same services are brought
# back up, and row counts are compared with the backup's. `restore --test` only
# checks the snapshot and leaves the live stack running.
# A restore rotates the JWT secret (every session ends), expires pending
# approvals, and takes users/credentials from the backup date.
#
# Env: VIGIL_REPO_DIR (~/vigil)  COMPOSE_FILE  ENV_FILE ($VIGIL_REPO_DIR/.env)
#      BACKUP_ROOT (~/vigil-compose-backups)  COMPOSE_PROJECT_NAME (auto-detected
#      from the running postgres container)
#      VIGIL_BACKUP_STATE_DIR / VIGIL_BACKUP_INVESTIGATIONS_DIR: host paths, only
#      if state is NOT in the vigil_home / vigil_investigations named volumes.
set -Eeuo pipefail
umask 077

VIGIL_REPO_DIR="${VIGIL_REPO_DIR:-$HOME/vigil}"
COMPOSE_FILE="${COMPOSE_FILE:-$VIGIL_REPO_DIR/infra/docker/docker-compose.yml}"
ENV_FILE="${ENV_FILE:-$VIGIL_REPO_DIR/.env}"
BACKUP_ROOT="${BACKUP_ROOT:-$HOME/vigil-compose-backups}"
REPO="$BACKUP_ROOT/restic"
PASSFILE="$BACKUP_ROOT/passphrase"
IMAGE=vigil-backend:local
PG_USER=deeptempo; PG_DB=deeptempo_soc
KEEP_STOPPED_ALWAYS=(postgres redis)
SELF="$(basename "${BASH_SOURCE[0]:-vigil-compose.sh}")"; [ "$SELF" = bash ] && SELF=vigil-compose.sh

log()  { printf '%s [compose] %s\n' "$(date -u +%T)" "$*"; }
warn() { printf '%s [compose] WARNING: %s\n' "$(date -u +%T)" "$*" >&2; }
die()  { printf '%s [compose] ERROR: %s\n' "$(date -u +%T)" "$*" >&2; exit 1; }

# ---------- plumbing ----------
load_env() {
    # Same rule as start.sh: a JWT the caller exported wins; one in .env must not
    # be turned into an exported variable that outranks secrets.enc.
    local caller_jwt="${JWT_SECRET_KEY:-}"
    # tr: a .env saved by a Windows editor has CRLF line endings.
    if [ -f "$ENV_FILE" ]; then set -a; eval "$(tr -d '\r' < "$ENV_FILE")"; set +a; fi
    if [ -n "$caller_jwt" ]; then export JWT_SECRET_KEY="$caller_jwt"; else unset JWT_SECRET_KEY; fi
}
detect_project() {
    [ -n "${COMPOSE_PROJECT_NAME:-}" ] && return 0
    COMPOSE_PROJECT_NAME="$(docker inspect -f '{{index .Config.Labels "com.docker.compose.project"}}' \
        deeptempo-postgres 2>/dev/null || true)"
    [ -n "$COMPOSE_PROJECT_NAME" ] || die "deeptempo-postgres is not running; start the stack or set COMPOSE_PROJECT_NAME"
    export COMPOSE_PROJECT_NAME
}
dc() { docker compose -f "$COMPOSE_FILE" "$@"; }
psql_q() { docker exec deeptempo-postgres psql -U "$PG_USER" -d "$PG_DB" -Atc "$1"; }
counts() {
    psql_q "select 'findings='||count(*) from findings union all
            select 'cases='||count(*) from cases union all
            select 'case_findings='||count(*) from case_findings union all
            select 'approval_actions='||count(*) from approval_actions union all
            select 'users='||count(*) from users union all
            select 'workflow_runs='||count(*) from workflow_runs"
}

preflight() {
    command -v docker >/dev/null && docker info >/dev/null 2>&1 || die "docker is not reachable by $(id -un)"
    [ -f "$COMPOSE_FILE" ] || die "no compose file at $COMPOSE_FILE"
    docker image inspect "$IMAGE" >/dev/null 2>&1 \
        || die "image $IMAGE missing: bring the stack up once (backup-pre-upgrade builds it)"
    [ "$(docker inspect -f '{{.State.Health.Status}}' deeptempo-postgres 2>/dev/null)" = healthy ] \
        || die "deeptempo-postgres is not healthy"
    detect_project
    load_env
    # Refuse to snapshot an empty state volume and call it a backup.
    if [ -z "${VIGIL_BACKUP_STATE_DIR:-}" ]; then
        local vol="${COMPOSE_PROJECT_NAME}_vigil_home"
        docker volume inspect "$vol" >/dev/null 2>&1 || die "volume $vol does not exist. If state lives in host directories (a start.sh install), use ops/vigil-backup.sh, or set VIGIL_BACKUP_STATE_DIR and VIGIL_BACKUP_INVESTIGATIONS_DIR."
        docker run --rm -v "$vol:/s:ro" --entrypoint sh "$IMAGE" -c 'ls /s/master.key >/dev/null 2>&1' \
            || die "volume $vol has no master.key: this is not where this stack keeps its state"
    else
        [ -f "$VIGIL_BACKUP_STATE_DIR/master.key" ] || die "$VIGIL_BACKUP_STATE_DIR has no master.key"
    fi
}

# The backup service runs as the image's user. A host bind-mounted repo must be
# writable by that uid, and readable by us afterwards.
CUID="" CGID=""
image_ids() { CUID="$(docker run --rm --entrypoint id "$IMAGE" -u)"; CGID="$(docker run --rm --entrypoint id "$IMAGE" -g)"; }
chown_repo() { # $1 uid:gid
    [ "$CUID" = "$(id -u)" ] && return 0
    docker run --rm --user 0 -v "$REPO:/r" --entrypoint chown "$IMAGE" -R "$1" /r
}
prep_repo() {
    mkdir -p "$REPO"; chmod 700 "$BACKUP_ROOT"
    if [ ! -f "$PASSFILE" ]; then
        head -c 32 /dev/urandom | base64 > "$PASSFILE"
        warn "created $PASSFILE. COPY IT OFF THIS HOST NOW: without it every backup is unreadable."
    fi
    # Readable by the container uid; the 0700 parent keeps everyone else out.
    [ "$CUID" = "$(id -u)" ] && chmod 600 "$PASSFILE" || chmod 444 "$PASSFILE"
    chown_repo "$CUID:$CGID"
}
# One-shot of the compose `backup` service with the entrypoint replaced.
core_backup() { # args after `python -m core.backup`
    VIGIL_BACKUP_REPO="$REPO" VIGIL_BACKUP_PASSPHRASE_FILE="$PASSFILE" \
        dc run --rm --no-deps -T --entrypoint python backup -m core.backup "$@"
}
COMMON=(--repo /backup/repo --passphrase-file /backup/passphrase --bifrost-data /var/lib/vigil/bifrost)

# ---------- backup ----------
do_backup() {
    local verify=1 keep=14 offsite="" dry=0
    while [ $# -gt 0 ]; do
        case "$1" in
            --no-verify) verify=0 ;; --dry-run) dry=1 ;;
            --keep) keep="${2:?}"; shift ;; --offsite) offsite="${2:?}"; shift ;;
            *) die "unknown option: $1" ;;
        esac; shift
    done
    preflight
    local ts run_dir dump extras db_bytes free_kb
    ts="$(date -u +%Y%m%dT%H%M%SZ)"; run_dir="$BACKUP_ROOT/runs/$ts"
    dump="$run_dir/vigil-$ts.pgdump"; extras="$run_dir/vigil-extras-$ts.tar.enc"
    mkdir -p "$BACKUP_ROOT"
    db_bytes="$(psql_q "select pg_database_size('$PG_DB')")"
    free_kb="$(df -Pk "$BACKUP_ROOT" | awk 'NR==2{print $4}')"
    [ "$free_kb" -gt $(( db_bytes / 1024 * 3 + 2097152 )) ] || die "not enough free space under $BACKUP_ROOT"
    log "project $COMPOSE_PROJECT_NAME, db $((db_bytes / 1048576)) MiB, $((free_kb / 1024)) MiB free"
    [ "$dry" = 1 ] && { log "dry run: preflight passed, nothing written"; return 0; }

    exec 9>"$BACKUP_ROOT/.lock"; flock -n 9 || die "another backup is running"
    mkdir -p "$run_dir"; image_ids; prep_repo

    log "native snapshot (compose backup service)"
    local out snap
    out="$(core_backup create "${COMMON[@]}" 2>"$run_dir/snapshot.log")" \
        || { chown_repo "$(id -u):$(id -g)"; tail -20 "$run_dir/snapshot.log" >&2; die "snapshot failed"; }
    snap="$(printf '%s\n' "$out" | grep -E '^[0-9a-f]{8,64}$' | tail -1 || true)"
    [ -n "$snap" ] || { chown_repo "$(id -u):$(id -g)"; die "no snapshot id in output: $out"; }
    log "snapshot $snap"

    if [ "$verify" = 1 ]; then
        log "restore --test (live state untouched)"
        VIGIL_BACKUP_REPO_MODE=ro core_backup restore "${COMMON[@]}" --snapshot "$snap" --test \
            >"$run_dir/verify.log" 2>&1 \
            || { chown_repo "$(id -u):$(id -g)"; tail -20 "$run_dir/verify.log" >&2; die "snapshot failed restore --test"; }
    fi
    chown_repo "$(id -u):$(id -g)"

    log "independent pg_dump"
    docker exec deeptempo-postgres pg_dump -U "$PG_USER" -d "$PG_DB" -Fc > "$dump"
    local toc; toc="$(docker exec -i deeptempo-postgres pg_restore --list < "$dump" | grep -c . || true)"
    [ "$toc" -gt 50 ] || die "dump looks empty ($toc TOC entries)"

    log "extras bundle"
    local stage; stage="$(mktemp -d "$run_dir/stage.XXXXXX")"
    [ -f "$ENV_FILE" ] && cp "$ENV_FILE" "$stage/dot-env"
    ( cd "$VIGIL_REPO_DIR" 2>/dev/null && { git rev-parse HEAD; git diff HEAD; } > "$stage/git.txt" ) || true
    cp "$VIGIL_REPO_DIR/VERSION" "$stage/VERSION" 2>/dev/null || true
    docker images --format '{{.Repository}}:{{.Tag}} {{.ID}}' > "$stage/images.txt"
    docker volume ls --format '{{.Name}}' | grep "^${COMPOSE_PROJECT_NAME}_" > "$stage/volumes.txt" || true
    dc ps --format '{{.Service}} {{.State}}' > "$stage/services.txt" 2>/dev/null || true
    tar -C "$stage" -cf - . | openssl enc -aes-256-cbc -pbkdf2 -salt -pass "file:$PASSFILE" -out "$extras"
    rm -rf "$stage"
    openssl enc -d -aes-256-cbc -pbkdf2 -pass "file:$PASSFILE" -in "$extras" | tar -tf - >/dev/null \
        || die "extras bundle does not decrypt"

    counts > "$run_dir/counts.txt"
    ( cd "$run_dir" && sha256sum "$(basename "$dump")" "$(basename "$extras")" > SHA256SUMS )
    printf 'SNAPSHOT_ID=%s\nTIMESTAMP=%s\nPROJECT=%s\nVERSION=%s\n' \
        "$snap" "$ts" "$COMPOSE_PROJECT_NAME" "$(cat "$VIGIL_REPO_DIR/VERSION" 2>/dev/null || echo ?)" > "$run_dir/run.env"
    ln -sfn "$run_dir" "$BACKUP_ROOT/latest"
    ls -1dt "$BACKUP_ROOT"/runs/*/ | tail -n +$((keep + 1)) | xargs -r rm -rf --

    if [ -n "$offsite" ]; then
        log "copying to $offsite (passphrase is NOT copied)"
        rsync -a "$run_dir" "$offsite/runs/" && rsync -a "$REPO/" "$offsite/restic/"
    else
        warn "no --offsite: this backup shares a host with the stack it protects"
    fi
    log "DONE snapshot=$snap dir=$run_dir"
    echo "restore with:  $SELF restore --snapshot $snap"
}

# ---------- restore ----------
do_restore() {
    local snap=latest test=0 yes=0 clear_jwt=0
    while [ $# -gt 0 ]; do
        case "$1" in
            --snapshot) snap="${2:?}"; shift ;; --test) test=1 ;; --yes) yes=1 ;;
            --clear-env-jwt) clear_jwt=1 ;; *) die "unknown option: $1" ;;
        esac; shift
    done
    [[ "$snap" =~ ^(latest|[0-9a-f]{8,64})$ ]] || die "bad snapshot id: $snap"
    [ -d "$REPO" ] && [ -f "$PASSFILE" ] || die "no repo/passphrase under $BACKUP_ROOT"
    preflight; image_ids

    if [ "$test" = 1 ]; then
        chown_repo "$CUID:$CGID"
        local rc=0
        VIGIL_BACKUP_REPO_MODE=ro core_backup restore "${COMMON[@]}" --snapshot "$snap" --test || rc=$?
        chown_repo "$(id -u):$(id -g)"
        [ "$rc" -eq 0 ] && log "snapshot $snap passes restore --test"
        return "$rc"
    fi

    [ "$yes" = 1 ] || { printf 'Restore snapshot %s? The stack goes DOWN; everything since the backup is lost. [y/N] ' "$snap"
                        read -r a; [ "$a" = y ] || die "aborted"; }

    local ts run_dir; ts="$(date -u +%Y%m%dT%H%M%SZ)"; run_dir="$BACKUP_ROOT/pre-restore/$ts"
    mkdir -p "$run_dir"
    log "safety dump of the CURRENT database -> $run_dir"
    docker exec deeptempo-postgres pg_dump -U "$PG_USER" -d "$PG_DB" -Fc > "$run_dir/current.pgdump"
    docker exec -i deeptempo-postgres pg_restore --list < "$run_dir/current.pgdump" >/dev/null \
        || die "safety dump unreadable; refusing to restore"

    # Remember what was running, then stop all of it except Postgres/Redis.
    mapfile -t running < <(dc ps --services --status running)
    local stop=() s keep
    for s in "${running[@]}"; do
        keep=0; for k in "${KEEP_STOPPED_ALWAYS[@]}"; do [ "$s" = "$k" ] && keep=1; done
        [ "$keep" = 0 ] && stop+=("$s")
    done
    printf '%s\n' "${stop[@]}" > "$run_dir/stopped-services.txt"
    log "stopping: ${stop[*]}"
    [ "${#stop[@]}" -gt 0 ] && dc stop "${stop[@]}"
    # A restart-policy loop container must not hold the database lock.
    docker rm -f vigil-backup-loop >/dev/null 2>&1 || true

    chown_repo "$CUID:$CGID"
    local rc=0
    core_backup restore "${COMMON[@]}" --snapshot "$snap" | tee "$run_dir/restore.out" || rc=${PIPESTATUS[0]}
    chown_repo "$(id -u):$(id -g)"
    if [ "$rc" -ne 0 ]; then
        warn "restore failed; a failure before the swap leaves the live data in place. Bringing the stack back up."
        [ "${#stop[@]}" -gt 0 ] && dc up -d "${stop[@]}" || true
        die "restore failed (see $run_dir/restore.out). Safety dump: $run_dir/current.pgdump"
    fi

    # A JWT in the environment outranks the rotated one in secrets.enc.
    if [ -n "${JWT_SECRET_KEY:-}" ] || grep -Eq '^\s*(export\s+)?JWT_SECRET_KEY\s*=' "$ENV_FILE" 2>/dev/null; then
        if [ "$clear_jwt" = 1 ] && [ -f "$ENV_FILE" ]; then
            cp "$ENV_FILE" "$run_dir/dot-env.before"
            sed -i -E 's/^(\s*(export\s+)?JWT_SECRET_KEY\s*=)/# cleared by restore: \1/' "$ENV_FILE"
            log "commented out JWT_SECRET_KEY in $ENV_FILE (copy: $run_dir/dot-env.before)"
        else
            warn "JWT_SECRET_KEY is set in $ENV_FILE / the environment and outranks the rotated key; clear it (or rerun with --clear-env-jwt) or logins will fail"
        fi
    fi

    log "starting: ${stop[*]}"
    dc up -d "${stop[@]}"
    local t=0
    until [ "$(docker inspect -f '{{.State.Health.Status}}' deeptempo-backend 2>/dev/null)" = healthy ] || [ "$t" -ge 600 ]; do sleep 5; t=$((t + 5)); done
    [ "$t" -lt 600 ] || warn "backend not healthy after 10 min: check 'docker compose logs backend'"

    local want="" d
    for d in "$BACKUP_ROOT"/runs/*/; do
        grep -q "^SNAPSHOT_ID=${snap}" "$d/run.env" 2>/dev/null && want="$d/counts.txt"
    done
    [ "$snap" = latest ] && want="$BACKUP_ROOT/latest/counts.txt"
    if [ -f "$want" ]; then
        diff <(cat "$want") <(counts) >/dev/null && log "row counts match the backup" \
            || { warn "row counts differ from the backup:"; diff <(cat "$want") <(counts) || true; }
    else
        log "no recorded counts for $snap (not taken by this script); spot-check the UI"
    fi
    log "RESTORED $snap. Sessions were ended; pending approvals expired. Safety dump kept: $run_dir/current.pgdump"
}

do_list() {
    [ -d "$REPO" ] || die "no repo at $REPO"
    for d in "$BACKUP_ROOT"/runs/*/; do [ -f "$d/run.env" ] && tr '\n' ' ' < "$d/run.env" && echo; done
}

case "${1:-}" in
    backup)  shift; do_backup "$@" ;;
    restore) shift; do_restore "$@" ;;
    list)    do_list ;;
    -h|--help|"") sed -n '2,28p' "$0" ;;
    *) die "unknown command: $1 (backup | restore | list)" ;;
esac
