#!/usr/bin/env bash
# EXPERIMENTAL: this script is experimental and has only been tested on
# Debian 13 and compatible Linux builds. Other distributions may require
# modification.
#
# Vigil release upgrade for a start.sh / systemd install.
# Run as root (it stops/starts vigil.service); git, pip, npm and start.sh run
# as the vigil user.
#
#   vigil-upgrade.sh [vX.Y.Z]      upgrade to a release tag (default: newest stable)
#   vigil-upgrade.sh --check       report only: target, path, local-change overlap
#   vigil-upgrade.sh --rollback    restore the backup this script took and put the
#                                  previous release and local edits back
# Options: --yes  skip the prompt          --skip-backup  (needs --i-have-a-backup)
#          --reapply-local  try to re-apply this host's uncommitted edits
#          --auto-rollback  restore automatically if the schema changed and
#                           the new release will not come up healthy
#
# Order, and why:
#   1 preflight  target tag exists upstream, no downgrade, 0.7.0+ (docs/upgrading.md),
#                API healthy now, disk, record row counts and alembic state.
#   2 backup     vigil-backup.sh: verified snapshot + dump. No backup, no upgrade.
#   3 stop       maintenance window starts. Postgres/Redis/Bifrost containers stay up.
#   4 code       stash local edits (also saved as a patch), check out the tag.
#   5 deps       pip install -r requirements.lock, npm ci.
#   6 start      start.sh runs `core.upgrade`, which takes its own pre-upgrade
#                snapshot first and REFUSES (changing nothing) on schema drift.
#   7 verify     health, VERSION, alembic_version, no table lost rows.
#   On failure: schema unchanged -> revert code only. Schema changed -> restore
#   the backup with the old release (never roll back by image tag alone). The
#   rollback rebuilds vigil-backend:local first: `start.sh restore` runs in it.
set -Eeuo pipefail
umask 077

VIGIL_DIR="${VIGIL_DIR:-/home/vigil/vigil}"
BACKUP_ROOT="${BACKUP_ROOT:-/home/vigil/vigil-backups}"
BACKUP_SCRIPT="${VIGIL_BACKUP_SCRIPT:-$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)/vigil-backup.sh}"
PG_CONTAINER="${PG_CONTAINER:-deeptempo-postgres}"
BIFROST_CONTAINER="${BIFROST_CONTAINER:-deeptempo-bifrost}"
PG_USER="${PG_USER:-deeptempo}"; PG_DB="${PG_DB:-deeptempo_soc}"
HEALTH_URL="${HEALTH_URL:-http://127.0.0.1:6987/api/health}"
HEALTH_WAIT="${HEALTH_WAIT:-900}"
STATE="$BACKUP_ROOT/upgrade-last.env"
DROPIN=/etc/systemd/system/vigil.service.d/upgrade-timeout.conf
AS=()
[ "$(id -u)" -eq 0 ] && AS=(runuser -u vigil --)

log()  { printf '%s [upgrade] %s\n' "$(date -u +%T)" "$*"; }
warn() { printf '%s [upgrade] WARNING: %s\n' "$(date -u +%T)" "$*" >&2; }
die()  { printf '%s [upgrade] ERROR: %s\n' "$(date -u +%T)" "$*" >&2; exit 1; }
V()    { ( cd "$VIGIL_DIR" && ${AS[@]+"${AS[@]}"} "$@" ); }
git_() { V git "$@"; }
psql_q() { docker exec "$PG_CONTAINER" psql -U "$PG_USER" -d "$PG_DB" -Atc "$1"; }
vlt()  { [ "$1" != "$2" ] && [ "$(printf '%s\n%s\n' "$1" "$2" | sort -V | head -1)" = "$1" ]; }
healthy() { [ "$(curl -s -m 5 -o /dev/null -w '%{http_code}' "$HEALTH_URL" || true)" = 200 ]; }
wait_healthy() {
    local t=0
    while [ "$t" -lt "$HEALTH_WAIT" ]; do healthy && return 0; sleep 5; t=$((t + 5)); done
    return 1
}
alembic_state() {
    # A 0.8.0 database has no alembic_version table; a query naming it would not parse.
    if [ "$(psql_q "select to_regclass('public.alembic_version') is not null")" = t ]; then
        psql_q "select coalesce(string_agg(version_num, ','), 'empty') from alembic_version"
    else
        echo none
    fi
}
counts() {
    psql_q "select 'findings='||count(*) from findings union all
            select 'cases='||count(*) from cases union all
            select 'case_findings='||count(*) from case_findings union all
            select 'approval_actions='||count(*) from approval_actions union all
            select 'users='||count(*) from users union all
            select 'workflow_runs='||count(*) from workflow_runs"
}
service_stop() {
    systemctl stop vigil
    local t=0; while healthy && [ "$t" -lt 60 ]; do sleep 2; t=$((t + 2)); done
    healthy && return 1
    return 0
}
service_start() {
    # start.sh runs the schema upgrade before it forks; the unit's 300s start
    # timeout can be too short for an adoption, so lift it for this run only.
    mkdir -p "$(dirname "$DROPIN")"
    printf '[Service]\nTimeoutStartSec=%s\n' "$HEALTH_WAIT" > "$DROPIN"
    systemctl daemon-reload
    local rc=0
    systemctl start vigil || rc=$?
    rm -f "$DROPIN"; systemctl daemon-reload
    [ "$rc" -eq 0 ] && wait_healthy
}
deps() {
    log "dependencies ($(cat "$VIGIL_DIR/VERSION"))"
    # The venv is made by uv and has no pip; install the way start.sh does.
    local uv; uv="$(command -v uv || true)"; [ -n "$uv" ] || uv="$HOME/.local/bin/uv"
    V "$uv" pip install -q --python ./venv/bin/python -r requirements.lock
    if [ -f "$VIGIL_DIR/clients/web/package-lock.json" ]; then
        ( cd "$VIGIL_DIR/clients/web" && ${AS[@]+"${AS[@]}"} npm ci --no-audit --no-fund --loglevel=error )
    fi
    if [ -f "$VIGIL_DIR/services/agent/package-lock.json" ]; then
        ( cd "$VIGIL_DIR/services/agent" && ${AS[@]+"${AS[@]}"} npm ci --no-audit --no-fund --loglevel=error && ${AS[@]+"${AS[@]}"} npm run build --silent )
    fi
}
# Put the recorded previous release and this host's local edits back.
revert_code() {
    # shellcheck disable=SC1090
    source "$STATE"
    log "reverting code to $PREV_VERSION ($PREV_REV)"
    git_ checkout --force --detach "$PREV_REV"
    if [ "${STASHED:-0}" = 1 ]; then
        local ref; ref="$(git_ stash list | awk -F: -v m="$STASH_MSG" 'index($0, m){print $1; exit}')"
        if [ -z "$ref" ]; then
            warn "stash '$STASH_MSG' not found; patch is in $RUN_DIR"
        elif git_ stash pop "$ref" >/dev/null; then
            log "local edits restored"
        else
            warn "stash pop failed; edits are still in '$STASH_MSG' and in $RUN_DIR/local-changes.patch"
        fi
    fi
    deps
}

rollback() {
    [ -f "$STATE" ] || die "no $STATE: nothing to roll back"
    # shellcheck disable=SC1090
    source "$STATE"
    log "ROLLBACK to $PREV_VERSION using snapshot $SNAPSHOT_ID"
    confirm "Restore the database and state from $SNAPSHOT_ID? Everything written since then is lost."
    service_stop || die "API still answering after stop"
    docker stop "$BIFROST_CONTAINER" >/dev/null 2>&1 || true   # restore replaces its data
    revert_code     # restore must run with the release that took the snapshot
    # `start.sh restore` runs core.backup inside vigil-backend:local, which the
    # upgrade rebuilt from the newer release. Rebuild it from the reverted tree,
    # or the newer models run against the older schema and the restore fails.
    log "rebuilding vigil-backend:local from $PREV_VERSION"
    V bash -c 'source scripts/lib.sh && dc build backup-pre-upgrade' >/dev/null \
        || die "could not rebuild vigil-backend:local; re-run --rollback"
    V ./start.sh restore --repo "$REPO" --passphrase-file "$PASSFILE" --snapshot "$SNAPSHOT_ID" \
        || die "restore failed; the snapshot and passphrase are untouched, fix and re-run --rollback"
    service_start || die "previous release did not come up healthy; check logs/ and journalctl -u vigil"
    verify_counts_equal
    log "ROLLED BACK to $PREV_VERSION. Schema now: $(alembic_state)"
}
verify_counts_equal() {
    local want have
    want="$(cat "$RUN_DIR/counts.txt")"; have="$(counts)"
    [ "$want" = "$have" ] || { warn "counts differ from backup:"; diff <(echo "$want") <(echo "$have") || true; return 1; }
}
confirm() {
    [ "$YES" = 1 ] && return 0
    printf '%s [y/N] ' "$1"; read -r a; [ "$a" = y ] || die "aborted"
}

main() {
    YES=0; local target="" check=0 do_rb=0 skip_backup=0 ack=0 reapply=0 auto_rb=0
    while [ $# -gt 0 ]; do
        case "$1" in
            --check) check=1 ;; --rollback) do_rb=1 ;; --yes) YES=1 ;;
            --skip-backup) skip_backup=1 ;; --i-have-a-backup) ack=1 ;;
            --reapply-local) reapply=1 ;; --auto-rollback) auto_rb=1 ;;
            -h|--help) sed -n '2,28p' "${BASH_SOURCE[0]:-$0}"; return 0 ;;
            v[0-9]*|[0-9]*) target="v${1#v}" ;;
            *) die "unknown argument: $1" ;;
        esac
        shift
    done
    [ "$skip_backup" = 1 ] && [ "$ack" != 1 ] && die "--skip-backup also needs --i-have-a-backup"
    [ "$check" = 1 ] || [ "$(id -u)" -eq 0 ] || die "run as root (stops and starts vigil.service)"
    [ -d "$VIGIL_DIR/.git" ] || die "$VIGIL_DIR is not a git checkout"
    [ "$do_rb" = 1 ] && { rollback; return 0; }

    # ---- 1. preflight ----
    local origin current tags
    origin="$(git_ remote get-url origin)"
    current="$(tr -d '[:space:]' < "$VIGIL_DIR/VERSION")"
    tags="$(git ls-remote --tags --refs "$origin" | awk '{sub("refs/tags/","",$2); print $2}' \
            | grep -E '^v[0-9]+\.[0-9]+\.[0-9]+$' | sort -V)"
    [ -n "$tags" ] || die "could not list release tags from $origin"
    [ -n "$target" ] || target="$(echo "$tags" | tail -1)"
    echo "$tags" | grep -qx "$target" || die "$target is not a release tag at $origin"
    local tver="${target#v}"
    log "current $current -> target $tver"
    [ "$tver" != "$current" ] || { log "already on $tver"; return 0; }
    vlt "$tver" "$current" && die "$tver is older than $current: downgrades are restores, not upgrades"
    vlt "$current" 0.7.0 && die "on $current: install a 0.7.x release first (docs/upgrading.md)"
    git_ describe --tags --exact-match HEAD >/dev/null 2>&1 \
        || warn "HEAD is not exactly a release tag: local commits will be left behind"

    git_ fetch --tags --quiet origin
    local dirty overlap
    dirty="$(git_ status --porcelain | grep -c . || true)"
    overlap="$(comm -12 <(git_ diff --name-only HEAD | sort) \
                         <(git_ diff --name-only HEAD "refs/tags/$target" | sort))"
    log "$dirty locally changed/untracked paths; $(git_ rev-list --count "HEAD..refs/tags/$target") upstream commits"
    if [ -n "$overlap" ]; then
        warn "local edits to files the release also changes (will NOT re-apply cleanly):"
        echo "$overlap" | sed 's/^/    /'
    fi
    healthy || die "API is not healthy now; fix that before changing anything"
    [ "$(docker inspect -f '{{.State.Health.Status}}' "$PG_CONTAINER")" = healthy ] || die "postgres not healthy"
    local free_gb; free_gb="$(df -Pk "$VIGIL_DIR" | awk 'NR==2{print int($4/1048576)}')"
    [ "$free_gb" -ge 5 ] || die "only ${free_gb} GiB free; need 5+"
    local pre_alembic; pre_alembic="$(alembic_state)"
    log "alembic before: $pre_alembic"
    [ "$check" = 1 ] && { log "check only: nothing changed (tags fetched)"; return 0; }
    confirm "Upgrade $current -> $tver now? Vigil is DOWN until it finishes (source events are not queued)."

    # ---- 2. backup ----
    local ts; ts="$(date -u +%Y%m%dT%H%M%SZ)"
    if [ "$skip_backup" = 1 ]; then
        warn "skipping the backup as instructed"
        [ -f "$BACKUP_ROOT/last-backup.env" ] || die "no earlier backup recorded either; refusing"
    else
        "$BACKUP_SCRIPT" || die "backup failed; nothing has been changed"
    fi
    # shellcheck disable=SC1091
    source "$BACKUP_ROOT/last-backup.env"
    local run_dir="$RUN_DIR" snap="$SNAPSHOT_ID" repo="$REPO" passfile="$PASSFILE"

    # ---- 3. stop ----
    local prev_rev stash_msg="vigil-upgrade-$ts" stashed=0
    prev_rev="$(git_ rev-parse HEAD)"
    {
        echo "PREV_REV=$prev_rev"; echo "PREV_VERSION=$current"; echo "TARGET=$tver"
        echo "SNAPSHOT_ID=$snap"; echo "REPO=$repo"; echo "PASSFILE=$passfile"
        echo "RUN_DIR=$run_dir"; echo "STASH_MSG=$stash_msg"; echo "STASHED=0"; echo "PRE_ALEMBIC=$pre_alembic"
    } > "$STATE"
    log "stopping vigil (maintenance window)"
    service_stop || die "API still answering after stop; nothing changed yet"

    # ---- 4. code ----
    git_ diff HEAD > "$run_dir/local-changes.patch" || true
    if [ "$dirty" -gt 0 ]; then
        # Tracked edits only: untracked files (ops/, generated vite.config.js, *.bak) stay in
        # place, so this script is not moved mid-run and a later pop cannot collide with them.
        git_ stash push -m "$stash_msg" >/dev/null && stashed=1
        sed -i "s/^STASHED=.*/STASHED=$stashed/" "$STATE"
    fi
    git_ checkout --detach "refs/tags/$target"
    [ "$(tr -d '[:space:]' < "$VIGIL_DIR/VERSION")" = "$tver" ] || die "checked out $target but VERSION disagrees"
    if [ "$stashed" = 1 ] && [ "$reapply" = 1 ]; then
        if git_ stash show -p "stash@{0}" 2>/dev/null | git_ apply --check - 2>/dev/null; then
            git_ stash pop >/dev/null && sed -i "s/^STASHED=.*/STASHED=0/" "$STATE" && log "local edits re-applied"
        else
            warn "local edits do not apply to $tver; left in git stash '$stash_msg' and $run_dir/local-changes.patch"
        fi
    elif [ "$stashed" = 1 ]; then
        log "local edits stashed ('$stash_msg'), not re-applied; review $run_dir/local-changes.patch"
    fi

    # ---- 5/6. deps + start (start.sh runs core.upgrade first) ----
    local ok=0
    if deps && service_start; then ok=1; fi

    # ---- 7. verify ----
    local post_alembic; post_alembic="$(alembic_state)"
    if [ "$ok" = 1 ]; then
        local bad=0
        [ "$post_alembic" != none ] && [ "$post_alembic" != empty ] || { warn "no alembic revision recorded"; bad=1; }
        while IFS== read -r t n; do
            m="$(counts | awk -F= -v t="$t" '$1==t{print $2}')"
            [ "${m:-0}" -ge "$n" ] || { warn "$t fell from $n to ${m:-0}"; bad=1; }
        done < "$run_dir/counts.txt"
        systemctl is-active --quiet vigil || { warn "vigil.service not active"; bad=1; }
        [ "$bad" = 0 ] || ok=0
    fi
    if [ "$ok" = 1 ]; then
        log "SUCCESS: $current -> $tver, alembic $post_alembic"
        log "keep $run_dir and the stash until you are happy; roll back later with: $0 --rollback"
        return 0
    fi

    warn "upgrade to $tver did not come up healthy (alembic $pre_alembic -> $post_alembic)"
    if [ "$post_alembic" = "$pre_alembic" ]; then
        log "schema unchanged: reverting code only"
        service_stop || true
        revert_code
        service_start || die "previous release will not start either; run: $0 --rollback"
        die "upgrade failed; running $current again with the database untouched. See journalctl -u vigil"
    fi
    if [ "$auto_rb" = 1 ]; then YES=1; rollback; die "upgrade failed and was rolled back"; fi
    die "schema changed: restore the backup with:  $0 --rollback"
}

main "$@"
exit
