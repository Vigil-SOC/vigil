#!/bin/sh
# Collect a support bundle for Vigil: one redacted tar.gz in the current
# directory. Portable POSIX sh, baseline tools only. Format: README.md.
#
#   vigil-support.sh [--mode native|compose|desktop] [--state-dir DIR] [--since DAYS]
#
# Exit 0: a bundle was written. Exit 1: none was.

set -u
umask 077
LC_ALL=C
export LC_ALL
unset ENABLE_KEYRING # nothing here may reach for the OS keychain
exec </dev/null      # never waits for input

HERE=$(cd "$(dirname "$0")" 2>/dev/null && pwd -P)
REDACT="$HERE/redact.awk"
NAMES="$HERE/secret-names.txt"
VERSION=unknown
[ -r "$HERE/VERSION" ] && read -r VERSION _ <"$HERE/VERSION"
case $VERSION in '' | *[!0-9A-Za-z.+_-]*) VERSION=unknown ;; esac # it is part of a file name

# Limits. The VIGIL_SUPPORT_* overrides exist for tests.
SRC_SECS=${VIGIL_SUPPORT_SOURCE_SECS:-60}
LOG_SECS=${VIGIL_SUPPORT_LOG_SECS:-90}
TOTAL_SECS=${VIGIL_SUPPORT_TOTAL_SECS:-300}
SRC_MAX=${VIGIL_SUPPORT_SOURCE_MAX:-52428800} # 50 MiB raw per source, newest kept
BUNDLE_MAX=1073741824 # 1 GiB raw per bundle
MIN_FREE_KB=$((BUNDLE_MAX / 1024))
FS_ROOT=${VIGIL_SUPPORT_FS_ROOT:-} # prefix for system paths, for tests
API_URL=${VIGIL_API_URL:-http://localhost:6987}

NOTICE="DATA NOTICE: this bundle holds information from this machine: its hostname,
the full process list with command lines, system logs and disk usage. Known
credential formats and secret names are redacted, but credentials in free log
text that match no known format cannot be guaranteed caught. Nothing is
uploaded. Review the bundle before you share it."

WORK=
RESERVED=
KEEP=0

die() {
    printf 'vigil-support: %s\n' "$*" >&2
    exit 1
}

has() { command -v "$1" >/dev/null 2>&1; }

# --- processes ---------------------------------------------------------------

kids() { ps -A -o pid= -o ppid= 2>/dev/null | awk -v p="$1" '$2 == p { print $1 }'; }

kill_tree() { # pid signal
    for _c in $(kids "$1"); do kill_tree "$_c" "$2"; done
    kill -"$2" "$1" 2>/dev/null
}

cleanup() {
    trap '' INT TERM HUP PIPE
    for _c in $(kids $$); do kill_tree "$_c" KILL; done
    [ "$KEEP" = 1 ] || { [ -n "$RESERVED" ] && rm -f "$RESERVED"; }
    [ -n "$WORK" ] && rm -rf "$WORK"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
trap 'exit 129' HUP
trap 'exit 141' PIPE

# --- arguments ---------------------------------------------------------------

MODE_ARG=
STATE_ARG=
SINCE=7
SINCE_SET=0
while [ $# -gt 0 ]; do
    case $1 in
    -h | --help)
        sed -n '2,8p' "$0" | sed 's/^# \{0,1\}//'
        exit 0
        ;;
    --mode | --state-dir | --since)
        [ $# -ge 2 ] || die "$1 needs a value"
        _opt=$1
        _val=$2
        shift 2
        ;;
    --mode=* | --state-dir=* | --since=*)
        _opt=${1%%=*}
        _val=${1#*=}
        shift
        ;;
    *) die "unknown option: $1 (try --help)" ;;
    esac
    case $_opt in
    --mode)
        case $_val in
        native | compose | desktop) MODE_ARG=$_val ;;
        *) die "--mode must be native, compose or desktop" ;;
        esac
        ;;
    --state-dir) STATE_ARG=$_val ;;
    --since)
        case $_val in
        '' | *[!0-9]* | 0) die "--since takes a whole number of days" ;;
        esac
        SINCE=$_val
        SINCE_SET=1
        ;;
    esac
done

OUTDIR=$(pwd -P)
[ -w "$OUTDIR" ] || die "cannot write to $OUTDIR"
[ -r "$REDACT" ] && [ -r "$NAMES" ] || die "redact.awk or secret-names.txt missing beside the script; refusing to collect without the redaction filter"
for _t in awk mkfifo mktemp tar tee tail wc tr; do
    has "$_t" || die "$_t not found; refusing to collect"
done

printf '%s\n\n' "$NOTICE"

# --- free space, before anything is created ----------------------------------

TMP_PARENT=${TMPDIR:-/tmp}
for _d in "$TMP_PARENT" "$OUTDIR"; do
    _kb=$(df -Pk "$_d" 2>/dev/null | awk 'NR == 2 { print $4 }')
    case $_kb in '' | *[!0-9]*) continue ;; esac
    [ "$_kb" -ge "$MIN_FREE_KB" ] ||
        die "not enough free space on $_d: ${_kb} KiB free, ${MIN_FREE_KB} KiB needed. Nothing written."
done

WORK=$(mktemp -d "$TMP_PARENT/vigil-support.XXXXXX") || die "cannot create a work directory in $TMP_PARENT"
RAW=$WORK/raw
ITEMS=$WORK/items.tsv
mkdir "$RAW"
: >"$ITEMS"
VALUES=$WORK/values.txt
: >"$VALUES"
T0=$(date +%s)
STAMP=${VIGIL_SUPPORT_NOW:-$(date -u +%Y%m%dT%H%M%SZ)}
CREATED=$(date -u +%Y-%m-%dT%H:%M:%SZ)
HOST_OS=$(uname -sr 2>/dev/null || echo unknown)
OS_KIND=$(uname -s 2>/dev/null || echo unknown)
ITEM=0
BUNDLE_RAW=0
EXTRA_CUT=0

# --- running things with a time limit ----------------------------------------

# Portable timeout (macOS has none): wait for background job PID while a
# watchdog beside it kills the job's tree when SECS pass. The job reports its
# status in STEM.st. Sets RC (124 on timeout).
supervise() { # secs stem pid
    (sleep "$1"; : >"$2.to"; kill_tree "$3" KILL) &
    _wd=$!
    { wait "$3"; } 2>/dev/null
    kill_tree "$_wd" KILL
    { wait "$_wd"; } 2>/dev/null
    if [ -s "$2.st" ]; then
        read -r RC <"$2.st"
    elif [ -e "$2.to" ]; then
        RC=124
    else
        RC=1
    fi
}

# Run a command under supervise. Its output is capped at SRC_MAX keeping the
# newest bytes; the true size comes through a fifo.
run_limited() { # secs stem cmd...
    _secs=$1
    _stem=$2
    shift 2
    {
        mkfifo "$_stem.fifo" || exit 1
        wc -c <"$_stem.fifo" >"$_stem.cnt" &
        ("$@" 2>"$_stem.err"; echo $? >"$_stem.st") | tee "$_stem.fifo" | tail -c "$SRC_MAX" >"$_stem.out"
        wait
    } &
    supervise "$_secs" "$_stem" $!
}

clean() { printf '%s' "$1" | tr -d '\000-\037'; }

record() { # path state reason source bytes bytes_cut redactions
    printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\n' "$1" "$2" "$(clean "$3")" "$(clean "$4")" \
        "${5:-0}" "${6:-0}" "${7:-0}" >>"$ITEMS"
}

skip() { record "$1" "not collected" "$2" "${3:-}"; } # path reason [source]

# A cut can land inside a private-key block whose BEGIN line is gone, which the
# filter would not recognise. Drop the partial first line and any such body.
trim_cut() { # file
    _e=$(grep -n -m1 -e '-----END [A-Z ]*PRIVATE KEY' "$1" | cut -d: -f1)
    _b=$(grep -n -m1 -e '-----BEGIN [A-Z ]*PRIVATE KEY' "$1" | cut -d: -f1)
    _k=1
    if [ -n "$_e" ] && { [ -z "$_b" ] || [ "$_e" -lt "$_b" ]; }; then _k=$_e; fi
    tail -n +$((_k + 1)) "$1" >"$1.t" && mv "$1.t" "$1"
}

# Redact a captured file into the bundle. A filter failure drops the item: the
# unredacted input is never copied.
store() { # dest stem source
    mkdir -p "$STAGE/$(dirname "$1")"
    _cf=$WORK/counts.$ITEM
    _seen=0
    [ -r "$2.cnt" ] && read -r _seen <"$2.cnt"
    _cut=$EXTRA_CUT
    [ "${_seen:-0}" -gt "$SRC_MAX" ] && _cut=$((_cut + _seen - SRC_MAX))
    [ "$_cut" -gt 0 ] && trim_cut "$2.out"
    rm -f "$2.r.st" "$2.r.to"
    (
        tr -d '\000' <"$2.out" |
            awk -f "$REDACT" -v names="$NAMES" -v values="$VALUES" -v counts="$_cf" -v name="$1" >"$STAGE/$1"
        echo $? >"$2.r.st"
    ) &
    supervise "$SRC_SECS" "$2.r" $!
    if [ "$RC" = 0 ]; then
        _n=0
        [ -r "$_cf" ] && IFS='	' read -r _ _n <"$_cf"
        BUNDLE_RAW=$((BUNDLE_RAW + $(wc -c <"$2.out")))
        _why=ok
        [ "$_cut" -gt 0 ] && _why="cut to the per-source size limit, newest kept"
        record "$1" collected "$_why" "$3" "$(wc -c <"$STAGE/$1" | tr -d ' ')" "$_cut" "$_n"
    else
        rm -f "$STAGE/$1"
        if [ "$RC" = 124 ]; then
            skip "$1" "redaction timed out after ${SRC_SECS} s; not copied unredacted" "$3"
        else
            skip "$1" "redaction failed (exit $RC); not copied unredacted" "$3"
        fi
    fi
}

# Exact secret values, for the filter's -v values=. Anything the filter would
# redact by name in this input is learned, so the same value is also caught
# where it shows up in free text (container logs, the process list).
LEARN=0
learn() { # file
    [ -s "$1" ] || return 0
    _lr=$WORK/learn
    tr -d '\000' <"$1" >"$_lr.in"
    awk -f "$REDACT" -v names="$NAMES" <"$_lr.in" >"$_lr.red" 2>/dev/null || return 0
    awk '
        NR == FNR { a[FNR] = $0; n = FNR; next }
        {
            m = FNR; o = a[FNR]; b = $0
            if (o == b) next
            la = length(o); lb = length(b); lim = la < lb ? la : lb
            for (p = 0; p < lim && substr(o, p + 1, 1) == substr(b, p + 1, 1); p++) ;
            for (s = 0; s < lim - p && substr(o, la - s, 1) == substr(b, lb - s, 1); s++) ;
            v = substr(o, p + 1, la - p - s)
            if (length(v) >= 6) out = out v "\n"
        }
        END { if (m == n) printf "%s", out }' "$_lr.in" "$_lr.red" >>"$VALUES"
    rm -f "$_lr.in" "$_lr.red"
}

# First line of a failed command's stderr (else stdout, for the ones that merge
# the two), through the filter: it goes into the manifest.
err_line() { # stem
    _el=$1.err
    [ -s "$_el" ] || _el=$1.out
    [ -s "$_el" ] || return 0
    sed -n 1p "$_el" | cut -c1-200 | tr -d '\000-\037' |
        awk -f "$REDACT" -v names="$NAMES" -v values="$VALUES" 2>/dev/null
}

ELEV_RE='permission denied|not permitted|insufficient permissions|must be root|access denied|administrator'

# collect_cmd DEST SECS SOURCE ELEV cmd args...   (ELEV=1: a refusal means "needs elevation")
collect_cmd() {
    _dest=$1
    _lim=$2
    _src=$3
    _elev=$4
    shift 4
    ITEM=$((ITEM + 1))
    if ! has "$1"; then
        skip "$_dest" "$1 not found" "$_src"
        return
    fi
    _left=$((TOTAL_SECS - ($(date +%s) - T0)))
    if [ "$_left" -le 0 ]; then
        skip "$_dest" "overall time limit of ${TOTAL_SECS} s reached" "$_src"
        return
    fi
    if [ "$BUNDLE_RAW" -ge "$BUNDLE_MAX" ]; then
        skip "$_dest" "bundle size limit of 1 GiB reached" "$_src"
        return
    fi
    [ "$_lim" -gt "$_left" ] && _lim=$_left
    _stem=$RAW/$ITEM
    run_limited "$_lim" "$_stem" "$@"
    _denied=0
    if [ "$_elev" = 1 ]; then
        # journalctl says so with exit 0, so a clean exit only counts for its message
        _re=$ELEV_RE
        [ "$RC" = 0 ] && _re='insufficient permissions'
        { cat "$_stem.err"; [ "$(wc -c <"$_stem.out")" -lt 2048 ] && cat "$_stem.out"; } 2>/dev/null |
            grep -qiE "$_re" && _denied=1
    fi
    if [ "$RC" = 124 ]; then
        skip "$_dest" "timed out after ${_lim} s" "$_src"
    elif [ "$_denied" = 1 ]; then
        skip "$_dest" "needs elevation" "$_src"
    elif [ "$RC" != 0 ]; then
        _e=$(err_line "$_stem")
        skip "$_dest" "exit status $RC${_e:+: $_e}" "$_src"
    else
        [ "$LEARN" = 1 ] && learn "$_stem.out"
        store "$_dest" "$_stem" "$_src"
    fi
    EXTRA_CUT=0
}

# collect_file DEST SRC ELEV ROOT...   A symlink is followed only to a regular
# file that is itself inside a ROOT; otherwise it is recorded, not followed.
collect_file() {
    _dest=$1
    _path=$2
    _elev=$3
    shift 3
    if [ ! -e "$_path" ] && [ ! -L "$_path" ]; then
        skip "$_dest" "$_path not found" "$_path"
        return
    fi
    if [ -L "$_path" ]; then
        _t=$(ls -ld "$_path" | sed 's/.* -> //')
        case $_t in /*) ;; *) _t=$(dirname "$_path")/$_t ;; esac
        _td=$(cd "$(dirname "$_t")" 2>/dev/null && pwd -P)
        _t=$_td/$(basename "$_t")
        _in=0
        for _r in "$@"; do
            _rp=$(cd "$_r" 2>/dev/null && pwd -P) || continue
            case $_t in "$_rp"/*) _in=1 ;; esac
        done
        if [ "$_in" = 0 ] || [ -L "$_t" ]; then
            skip "$_dest" "symlink to $_t leaves $*; recorded, not followed" "$_path"
            return
        fi
        _path=$_t
    fi
    case ${_path##*/} in
    vigil-support-*.tar.gz)
        skip "$_dest" "an earlier support bundle is never nested" "$_path"
        return
        ;;
    esac
    if [ ! -f "$_path" ]; then
        skip "$_dest" "not a regular file" "$_path"
        return
    fi
    if [ ! -r "$_path" ]; then
        if [ "$_elev" = 1 ]; then skip "$_dest" "needs elevation" "$_path"; else skip "$_dest" "not readable" "$_path"; fi
        return
    fi
    _size=$(wc -c <"$_path" | tr -d ' ')
    [ "$_size" -gt "$SRC_MAX" ] && EXTRA_CUT=$((_size - SRC_MAX))
    collect_cmd "$_dest" "$SRC_SECS" "$_path" 0 tail -c "$SRC_MAX" "$_path"
}

section() { printf '[%s] %s\n' "$1" "$2"; }

# --- detection ---------------------------------------------------------------

LOOKED=$WORK/looked.txt
INSTALLS=$WORK/installs.txt
: >"$LOOKED"
: >"$INSTALLS"

CHECKOUT=
find_checkout() {
    for _c in "${VIGIL_REPO_ROOT:-}" "$HERE/../.." "$(pwd -P)"; do
        [ -n "$_c" ] || continue
        if [ -f "$_c/infra/docker/docker-compose.yml" ]; then
            CHECKOUT=$(cd "$_c" && pwd -P)
            printf "checkout: found at %s\n" "$CHECKOUT" >>"$LOOKED"
            return
        fi
        printf "checkout: none at %s\n" "$_c" >>"$LOOKED"
    done
}

detect() {
    find_checkout
    SRC_NAMES=
    DESK_NAMES=
    SRC_PROJECT=
    SRC_CFG=
    DESK_CFG=
    if ! has docker; then
        echo "docker: not installed" >>"$LOOKED"
    else
        ITEM=$((ITEM + 1))
        run_limited 20 "$RAW/$ITEM" docker ps --format '{{.Names}}|{{.Label "com.docker.compose.project"}}|{{.Label "com.docker.compose.project.config_files"}}'
        if [ "$RC" != 0 ]; then
            printf "docker ps: failed (exit %s); containers not seen\n" "$RC" >>"$LOOKED"
        else
            while IFS='|' read -r _name _proj _cfg; do
                case $_name in
                deeptempo-*)
                    SRC_NAMES="$SRC_NAMES $_name"
                    [ -z "$SRC_PROJECT" ] && SRC_PROJECT=$_proj
                    [ -z "$SRC_CFG" ] && SRC_CFG=$_cfg
                    ;;
                esac
                if [ "$_proj" = vigil ]; then
                    DESK_NAMES="$DESK_NAMES $_name"
                    [ -z "$DESK_CFG" ] && DESK_CFG=$_cfg
                fi
            done <"$RAW/$ITEM.out"
            printf "docker ps: deeptempo-* containers:%s; Compose project vigil:%s\n" "${SRC_NAMES:- none}" "${DESK_NAMES:- none}" >>"$LOOKED"
        fi
    fi
    _want_src=1
    _want_desk=1
    case $MODE_ARG in
    native | compose) _want_desk=0 ;;
    desktop) _want_src=0 ;;
    esac
    if [ "$_want_src" = 1 ] && [ -n "$CHECKOUT" ] && [ -n "$SRC_NAMES" ]; then
        _m=${MODE_ARG:-native}
        [ -z "$MODE_ARG" ] && case $SRC_NAMES in *" deeptempo-backend"*) _m=compose ;; esac
        printf '%s\tfrom source, checkout %s, containers:%s\n' "$_m" "$CHECKOUT" "$SRC_NAMES" >>"$INSTALLS"
    fi
    if [ "$_want_desk" = 1 ] && [ -n "$DESK_NAMES" ]; then
        printf 'desktop\tDesktop standalone, Compose project vigil, containers:%s\n' "$DESK_NAMES" >>"$INSTALLS"
    fi
}

section 1/6 "detecting the install"
detect
N_INSTALLS=$(wc -l <"$INSTALLS" | tr -d ' ')
if [ "$N_INSTALLS" -gt 1 ]; then
    echo
    echo "More than one Vigil install was found, so nothing was written:"
    sed 's/^[^	]*	/  - /' "$INSTALLS"
    echo "Pick one with --mode native|compose|desktop and run again."
    exit 1
fi
MODE=host
INSTALL_NOTE="no Vigil install found"
if [ "$N_INSTALLS" = 1 ]; then
    IFS='	' read -r MODE INSTALL_NOTE <"$INSTALLS"
    KIND=install
else
    KIND=host
fi

if [ -n "$STATE_ARG" ]; then
    STATE_DIR=$STATE_ARG
elif [ "$MODE" = desktop ]; then
    case $OS_KIND in
    Darwin) STATE_DIR=${HOME:-}/Library/Application\ Support/Vigil ;;
    *) STATE_DIR=${HOME:-}/.config/Vigil ;;
    esac
else
    STATE_DIR=${VIGIL_DIR:-${HOME:-}/.vigil}
fi
if [ -d "$STATE_DIR" ]; then _found=found; else _found=none; fi
printf "state dir: %s at %s\n" "$_found" "$STATE_DIR" >>"$LOOKED"

NAME=vigil-support-$KIND-$VERSION-$STAMP
STAGE=$WORK/bundle/$NAME
mkdir -p "$STAGE/configuration" "$STAGE/health" "$STAGE/logs" "$STAGE/system"

# The API is asked for its version only; its payload is per-install collection.
HEALTH_VERSION=
if [ "$N_INSTALLS" = 1 ] && has curl; then
    ITEM=$((ITEM + 1))
    run_limited 10 "$RAW/$ITEM" curl -fsS --max-time 5 "$API_URL/api/health"
    [ "$RC" = 0 ] && HEALTH_VERSION=$(awk 'match($0, /"version" *: *"[^"]*"/) {
        v = substr($0, RSTART, RLENGTH); sub(/^"version" *: *"/, "", v); sub(/"$/, "", v); print v; exit }' "$RAW/$ITEM.out")
    case $HEALTH_VERSION in *[!0-9A-Za-z.+_-]*) HEALTH_VERSION= ;; esac # it lands in the manifest
fi

# --- per-install collection --------------------------------------------------

collect_secret_file() { # as collect_file; the file's secret values are learned first
    LEARN=1
    collect_file "$@"
    LEARN=0
}

collect_secret_cmd() { # as collect_cmd
    LEARN=1
    collect_cmd "$@"
    LEARN=0
}

# collect_where DEST NAME DIR...   the first DIR holding NAME wins
collect_where() {
    _wd=$1
    _wn=$2
    shift 2
    for _dir in "$@"; do
        [ -n "$_dir" ] || continue
        if [ -e "$_dir/$_wn" ] || [ -L "$_dir/$_wn" ]; then
            collect_file "$_wd" "$_dir/$_wn" 0 "$_dir"
            return
        fi
    done
    skip "$_wd" "$_wn not found in: $*"
}

# Existence only: the path and whether it is there, never its content.
never() { # label path [note]
    if [ -e "$2" ] || [ -L "$2" ]; then _p=present; else _p=absent; fi
    skip "configuration/never-included/$1" "never included; ${3:-$_p}" "$2"
}

# Containers of this install, from docker rather than a fixed list: KEPT holds
# names, EXCLUDED "name<TAB>state" for lab/demo containers and a user-run Ollama.
KEPT=$WORK/containers.kept
EXCLUDED=$WORK/containers.excluded
CONT_ERR=
list_containers() {
    : >"$KEPT"
    : >"$EXCLUDED"
    if ! has docker; then
        CONT_ERR="docker not found"
        return
    fi
    ITEM=$((ITEM + 1))
    run_limited 20 "$RAW/$ITEM" docker ps -a --format '{{.Names}}|{{.Label "com.docker.compose.project"}}|{{.State}}'
    if [ "$RC" != 0 ]; then
        CONT_ERR="docker ps -a failed (exit $RC)"
        return
    fi
    while IFS='|' read -r _name _proj _state; do
        case $_name in '' | *[!A-Za-z0-9_.-]*) continue ;; esac
        case $_name in
        deeptempo-splunk | deeptempo-kafka | deeptempo-elasticsearch | deeptempo-kibana | deeptempo-misp-* | deeptempo-pgadmin | *ollama*)
            printf '%s\t%s\n' "$_name" "${_state:-unknown}" >>"$EXCLUDED"
            continue
            ;;
        esac
        if [ "$MODE" = desktop ]; then
            [ "$_proj" = vigil ] || continue
        else
            case $_name in
            deeptempo-*) ;;
            *) [ -n "$SRC_PROJECT" ] && [ "$_proj" = "$SRC_PROJECT" ] || continue ;;
            esac
        fi
        echo "$_name" >>"$KEPT"
    done <"$RAW/$ITEM.out"
}

# Compose project and files of this install, from the container labels read by
# detect(), else the checkout's or the Desktop's own compose file.
COMPOSE_PROJECT=
COMPOSE_FILES=$WORK/compose-files.txt
find_compose_files() {
    : >"$COMPOSE_FILES"
    if [ "$MODE" = desktop ]; then
        COMPOSE_PROJECT=vigil
        _cfg=$DESK_CFG
        _def=$STATE_DIR/standalone/docker-compose.yml
    else
        COMPOSE_PROJECT=$SRC_PROJECT
        _cfg=$SRC_CFG
        _def=$CHECKOUT/infra/docker/docker-compose.yml
    fi
    _oifs=$IFS
    IFS=,
    for _f in $_cfg; do
        case $_f in *.yml | *.yaml) [ -f "$_f" ] && echo "$_f" >>"$COMPOSE_FILES" ;; esac
    done
    IFS=$_oifs
    [ -s "$COMPOSE_FILES" ] || { [ -f "$_def" ] && echo "$_def" >>"$COMPOSE_FILES"; }
}

collect_compose_config() {
    _dest=configuration/compose-config.yml
    if [ "$MODE" = native ]; then
        skip "$_dest" "native install: Compose is not what runs Vigil"
        return
    fi
    if ! has docker; then
        skip "$_dest" "docker not found"
        return
    fi
    if [ ! -s "$COMPOSE_FILES" ]; then
        skip "$_dest" "no compose file found"
        return
    fi
    set -- compose
    [ -n "$COMPOSE_PROJECT" ] && set -- "$@" -p "$COMPOSE_PROJECT"
    [ "$MODE" = compose ] && [ -r "$CHECKOUT/.env" ] && set -- "$@" --env-file "$CHECKOUT/.env"
    while IFS= read -r _f; do set -- "$@" -f "$_f"; done <"$COMPOSE_FILES"
    # A variable the app injects at launch (the Desktop's tokens) fails interpolation;
    # the uninterpolated file is still worth having.
    collect_secret_cmd "$_dest" "$SRC_SECS" "docker compose config" 0 sh -c \
        'docker compose "$@" config 2>/dev/null || docker compose "$@" config --no-interpolate' _ "$@"
}

collect_configuration() {
    # The files that hold secret values go first, so their values are known
    # before anything else is stored.
    if [ "$MODE" = desktop ]; then
        skip configuration/env "the Desktop has no .env"
    else
        collect_secret_file configuration/env "$CHECKOUT/.env" 0 "$CHECKOUT"
    fi
    collect_compose_config

    _ck=$CHECKOUT
    [ "$MODE" = desktop ] && _ck=
    collect_where configuration/state/backups.json backups.json "$STATE_DIR" "$_ck"
    collect_where configuration/state/mcp-config.json mcp-config.json "$_ck" "$STATE_DIR"
    collect_where configuration/state/INTENT.md INTENT.md "$_ck" "$STATE_DIR"
    collect_where configuration/state/vigil-autostart .vigil-autostart "$_ck" "$STATE_DIR"

    if [ "$MODE" = desktop ]; then
        while IFS= read -r _f; do
            collect_file "configuration/deployment/$(basename "$_f")" "$_f" 0 "$(dirname "$_f")"
        done <"$COMPOSE_FILES"
    else
        _d=$CHECKOUT/infra/docker
        for _f in docker-compose.yml otel-collector.yaml prometheus.yml bifrost/config.json; do
            collect_file "configuration/deployment/$_f" "$_d/$_f" 0 "$_d"
        done
        if [ -d "$_d/grafana" ]; then
            find "$_d/grafana" -type f \( -name '*.yml' -o -name '*.yaml' \) | sort >"$WORK/grafana.txt"
            while IFS= read -r _f; do
                collect_file "configuration/deployment/${_f#"$CHECKOUT"/infra/docker/}" "$_f" 0 "$_d"
            done <"$WORK/grafana.txt"
        fi
    fi

    never secrets.enc "$STATE_DIR/secrets.enc"
    never master.key "$STATE_DIR/master.key"
    never jwt_secret "$STATE_DIR/jwt_secret"
    never state-dir-env "$STATE_DIR/.env"
    never home-deeptempo-env "${HOME:-}/.deeptempo/.env"
    never bifrost-data "$STATE_DIR/bifrost"
    [ "$MODE" = desktop ] && never desktop-config "$STATE_DIR/config.json"
    if [ -r "$STATE_DIR/backups.json" ]; then
        awk '{ s = $0; while (match(s, /"repo" *: *"[^"]*"/)) {
            v = substr(s, RSTART, RLENGTH); sub(/^"repo" *: *"/, "", v); sub(/"$/, "", v); print v
            s = substr(s, RSTART + RLENGTH) } }' "$STATE_DIR/backups.json" | head -n 20 >"$WORK/repos.txt"
        _i=0
        while IFS= read -r _r; do
            _i=$((_i + 1))
            case $_r in
            /*) never "backup-repository-$_i" "$_r" ;;
            *) never "backup-repository-$_i" "$_r" "not a local path, not checked" ;;
            esac
        done <"$WORK/repos.txt"
    fi
}

collect_health() {
    _api=${API_URL%/}
    for _h in "api.json|$_api/api/health" \
        "daemon-health.json|http://localhost:9091/health" \
        "daemon-status.json|http://localhost:9091/status" \
        "webhook-health.json|http://localhost:8081/health" \
        "agent-serve-healthz.txt|http://localhost:6989/healthz" \
        "agent-worker-healthz.txt|http://localhost:6990/healthz" \
        "bifrost-health.json|http://localhost:8080/health"; do
        _url=${_h#*|}
        collect_cmd "health/${_h%%|*}" "$SRC_SECS" "GET $(printf '%s' "$_url" | sed 's|//[^/@]*@|//|')" 0 \
            curl -fsS --max-time 5 "$_url"
    done
    if [ -n "$CONT_ERR" ]; then
        skip health/containers.txt "$CONT_ERR"
    elif [ ! -s "$KEPT" ]; then
        skip health/containers.txt "no containers of this install"
    else
        # --format only: raw inspect output and container environments stay out.
        collect_cmd health/containers.txt "$SRC_SECS" "docker inspect --format" 0 sh -c \
            'for n; do docker inspect --format "{{.Name}} state={{.State.Status}} restarts={{.RestartCount}} started={{.State.StartedAt}} image={{.Config.Image}}" "$n" 2>&1; done' _ $(cat "$KEPT")
    fi
}

# collect_tree DEST_PREFIX DIR [find tests...]   every matching file under DIR, in place
collect_tree() {
    _pre=$1
    _root=$2
    shift 2
    if [ ! -d "$_root" ]; then
        skip "$_pre/" "$_root not found"
        return
    fi
    find "$_root" \( -type f -o -type l \) "$@" | sort >"$WORK/tree.txt"
    while IFS= read -r _f; do
        collect_file "$_pre/${_f#"$_root"/}" "$_f" 0 "$_root"
    done <"$WORK/tree.txt"
}

collect_logs() {
    case $MODE in
    native | compose)
        collect_tree logs/checkout "$CHECKOUT/logs"
        collect_file logs/state/vigil.log "$STATE_DIR/vigil.log" 0 "$STATE_DIR"
        ;;
    desktop)
        if [ "$OS_KIND" = Darwin ] && [ -z "$STATE_ARG" ]; then _dl=${HOME:-}/Library/Logs/Vigil; else _dl=$STATE_DIR/logs; fi
        collect_tree logs/desktop "$_dl" \( -name 'vigil-desktop.log*' -o \( -path '*/containers/*' -name 'vigil-*.log' \) \)
        ;;
    esac
    if [ "$MODE" = native ]; then
        skip logs/docker/ "native install: container logs are saved under logs/ by start.sh and shutdown"
    elif [ -n "$CONT_ERR" ]; then
        skip logs/docker/ "$CONT_ERR"
    else
        while IFS= read -r _n; do
            collect_cmd "logs/docker/$_n.log" "$LOG_SECS" "docker logs --since ${SINCE}d" 0 sh -c \
                'exec docker logs --timestamps --since "$1" "$2" 2>&1' _ "$((SINCE * 24))h" "$_n"
        done <"$KEPT"
    fi
    if [ ! -s "$KEPT" ] && [ -z "$CONT_ERR" ] && [ "$MODE" != native ]; then
        skip logs/docker/ "no containers of this install"
    fi
}

section 2/6 "collecting configuration, health and logs"
if [ "$N_INSTALLS" = 1 ]; then
    list_containers
    find_compose_files
    collect_configuration
    collect_health
    collect_logs
    while IFS='	' read -r _n _s; do
        skip "logs/docker/$_n.log" "excluded: lab/demo container; state $_s" "docker ps -a"
    done <"$EXCLUDED"
else
    for _p in configuration health logs; do skip "$_p/" "no Vigil install found"; done
fi

# --- system ------------------------------------------------------------------

section 3/6 "host, clock and processes"
collect_cmd system/host.txt "$SRC_SECS" "hostname; uname -a" 0 sh -c 'hostname; uname -a'
collect_cmd system/clock.txt "$SRC_SECS" "date" 0 sh -c 'date -u; date; date +%Z'
if has timedatectl; then
    collect_cmd system/timezone-sync.txt "$SRC_SECS" "timedatectl" 0 timedatectl
elif has systemsetup; then
    collect_cmd system/timezone-sync.txt "$SRC_SECS" "systemsetup" 0 sh -c 'systemsetup -gettimezone; systemsetup -getusingnetworktime'
else
    skip system/timezone-sync.txt "neither timedatectl nor systemsetup found"
fi
collect_cmd system/processes.txt "$SRC_SECS" "ps -ef" 0 ps -ef
collect_cmd system/processes-vigil.txt "$SRC_SECS" "ps -ef, Vigil entries" 0 \
    sh -c "ps -ef | awk 'NR == 1 || /[v]igil|[d]eeptempo/'"

section 4/6 "disk space and OS release"
collect_disk() {
    set -- df -Pk
    for _d in "$STATE_DIR" "$OUTDIR" "$TMP_PARENT"; do
        [ -d "$_d" ] && set -- "$@" "$_d"
    done
    if [ -n "$CHECKOUT" ]; then
        for _d in "$CHECKOUT/logs" "$CHECKOUT/data"; do
            [ -d "$_d" ] && set -- "$@" "$_d"
        done
    fi
    collect_cmd system/disk.txt "$SRC_SECS" "df -Pk, Vigil write locations" 0 "$@"
}
collect_disk
if [ -r "$FS_ROOT/etc/os-release" ] || [ -L "$FS_ROOT/etc/os-release" ]; then
    collect_file system/os-release.txt "$FS_ROOT/etc/os-release" 0 "$FS_ROOT/etc" "$FS_ROOT/usr/lib"
elif has sw_vers; then
    collect_cmd system/os-release.txt "$SRC_SECS" "sw_vers" 0 sw_vers
else
    skip system/os-release.txt "no /etc/os-release and no sw_vers"
fi

section 5/6 "system logs"
if [ "$OS_KIND" = Darwin ]; then
    _last=24h
    [ "$SINCE_SET" = 1 ] && _last=${SINCE}d
    collect_cmd system/macos-log.txt "$LOG_SECS" "log show --last $_last, Docker and Vigil entries" 1 \
        log show --style compact --last "$_last" --predicate \
        'process CONTAINS[c] "docker" OR process CONTAINS[c] "vigil" OR eventMessage CONTAINS[c] "docker" OR eventMessage CONTAINS[c] "vigil"'
    for _f in journal syslog kernel; do skip "system/$_f.txt" "Linux only"; done
else
    collect_cmd system/journal.txt "$LOG_SECS" "journalctl --since '$SINCE days ago'" 1 \
        journalctl --no-pager --since "$SINCE days ago"
    _sys=
    for _f in /var/log/syslog /var/log/messages; do
        [ -e "$FS_ROOT$_f" ] || [ -L "$FS_ROOT$_f" ] && { _sys=$FS_ROOT$_f; break; }
    done
    if [ -n "$_sys" ]; then
        collect_file system/syslog.txt "$_sys" 1 "$FS_ROOT/var/log"
    else
        skip system/syslog.txt "no /var/log/syslog or /var/log/messages"
    fi
    collect_cmd system/kernel.txt "$LOG_SECS" "dmesg" 1 dmesg
    skip system/macos-log.txt "macOS only"
fi

# --- manifest and summary ----------------------------------------------------

section 6/6 "writing the manifest and summary"
M_VERSION=$VERSION M_CREATED=$CREATED M_MODE=$MODE M_OS=$HOST_OS M_NOTE=$INSTALL_NOTE \
    M_HEALTH=$HEALTH_VERSION M_LOOKED=$LOOKED M_FOUND=$N_INSTALLS \
    awk -F '\t' '
    function q(s) { gsub(/[^ -~]/, "?", s); gsub(/\\/, "\\\\", s); gsub(/"/, "\\\"", s); return "\"" s "\"" }
    BEGIN {
        printf "{\n  \"format_version\": 1,\n  \"support_version\": %s,\n  \"created_utc\": %s,\n", q(ENVIRON["M_VERSION"]), q(ENVIRON["M_CREATED"])
        printf "  \"mode\": %s,\n  \"host_os\": %s,\n", q(ENVIRON["M_MODE"]), q(ENVIRON["M_OS"])
        printf "  \"install\": {\"found\": %s, \"description\": %s, \"api_version\": ", (ENVIRON["M_FOUND"] > 0 ? "true" : "false"), q(ENVIRON["M_NOTE"])
        printf "%s},\n  \"looked\": [", (ENVIRON["M_HEALTH"] == "" ? "null" : q(ENVIRON["M_HEALTH"]))
        sep = "\n    "
        while ((getline l < ENVIRON["M_LOOKED"]) > 0) { printf "%s%s", sep, q(l); sep = ",\n    " }
        printf "\n  ],\n  \"entries\": ["
        sep = "\n    "
    }
    {
        printf "%s{\"path\": %s, \"state\": %s, \"reason\": %s, \"source\": %s", sep, q($1), q($2), q($3), q($4)
        if ($2 == "collected") {
            printf ", \"bytes\": %d, \"redactions\": %d", $5, $7
            if ($6 > 0) printf ", \"bytes_cut\": %d", $6
        }
        printf "}"
        sep = ",\n    "
    }
    END { printf "\n  ]\n}\n" }' "$ITEMS" >"$STAGE/manifest.json" || die "cannot write manifest.json"

{
    echo "Vigil support bundle"
    printf 'Version:  %s\nMode:     %s (%s)\nHost OS:  %s\nCreated:  %s (UTC)\n' \
        "$VERSION" "$MODE" "$INSTALL_NOTE" "$HOST_OS" "$CREATED"
    if [ -n "$HEALTH_VERSION" ] && [ "$HEALTH_VERSION" != "$VERSION" ]; then
        printf 'Version mismatch: this tool is %s, /api/health reports %s\n' "$VERSION" "$HEALTH_VERSION"
    fi
    echo
    awk -F '\t' '$2 != "collected" { n++; l = l "  " $1 ": " $3 "\n" } END { printf "Not collected (%d)\n%s", n, l }' "$ITEMS"
    echo
    awk -F '\t' '$2 == "collected" { t += $7; if ($7 > 0) { l = l "  " $1 ": " $7 "\n"; k++ } }
        END { printf "Redactions: %d in %d files (other files: none)\n%s", t, k, l }' "$ITEMS"
    echo
    echo "Credentials in free log text that match no known format cannot be guaranteed caught."
    echo
    echo "$NOTICE"
} >"$STAGE/SUMMARY.txt" || die "cannot write SUMMARY.txt"

# --- pack --------------------------------------------------------------------

# A numeric suffix keeps concurrent runs from overwriting each other.
_n=0
FINAL=$OUTDIR/$NAME.tar.gz
until (
    set -C
    : >"$FINAL"
) 2>/dev/null; do
    _n=$((_n + 1))
    [ "$_n" -le 99 ] || die "cannot reserve a file name in $OUTDIR"
    FINAL=$OUTDIR/$NAME-$_n.tar.gz
done
RESERVED=$FINAL
COPYFILE_DISABLE=1 tar -czf "$FINAL" -C "$WORK/bundle" "$NAME" || die "tar failed; nothing written"
chmod 600 "$FINAL"
KEEP=1

SIZE=$(wc -c <"$FINAL" | tr -d ' ')
if has sha256sum; then
    SUM=$(sha256sum "$FINAL" | awk '{ print $1 }')
elif has shasum; then
    SUM=$(shasum -a 256 "$FINAL" | awk '{ print $1 }')
else
    SUM="unavailable (no sha256sum or shasum)"
fi

echo
echo "$NOTICE"
echo
printf 'Bundle:  %s\nSize:    %s bytes\nSHA-256: %s\n' "$FINAL" "$SIZE" "$SUM"
if awk -F '\t' '$2 != "collected" { f = 1 } END { exit !f }' "$ITEMS"; then
    echo
    echo "Not collected:"
    awk -F '\t' '$2 != "collected" { print "  " $1 ": " $3 }' "$ITEMS"
    if awk -F '\t' '$3 == "needs elevation" { f = 1 } END { exit !f }' "$ITEMS"; then
        echo "Run with sudo to include the items marked \"needs elevation\"."
    fi
fi
exit 0
