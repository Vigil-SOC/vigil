#!/bin/sh
# Collect a support bundle for Vigil: one redacted tar.gz in the current
# directory. Portable POSIX sh, baseline tools only. Format: README.md.
#
#   vigil-support.sh [--mode native|compose|desktop|helm] [--state-dir DIR] [--since DAYS]
#                    [--release NAME] [--namespace NS]   (the last two: --mode helm only)
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
REL_ARG=
NS_ARG=
SINCE=7
SINCE_SET=0
while [ $# -gt 0 ]; do
    case $1 in
    -h | --help)
        sed -n '2,8p' "$0" | sed 's/^# \{0,1\}//'
        exit 0
        ;;
    --mode | --state-dir | --since | --release | --namespace)
        [ $# -ge 2 ] || die "$1 needs a value"
        _opt=$1
        _val=$2
        shift 2
        ;;
    --mode=* | --state-dir=* | --since=* | --release=* | --namespace=*)
        _opt=${1%%=*}
        _val=${1#*=}
        shift
        ;;
    *) die "unknown option: $1 (try --help)" ;;
    esac
    case $_opt in
    --mode)
        case $_val in
        native | compose | desktop | helm) MODE_ARG=$_val ;;
        *) die "--mode must be native, compose, desktop or helm" ;;
        esac
        ;;
    --state-dir) STATE_ARG=$_val ;;
    --release | --namespace)
        case $_val in '' | *[!A-Za-z0-9._-]*) die "$_opt takes a Kubernetes name" ;; esac
        if [ "$_opt" = --release ]; then REL_ARG=$_val; else NS_ARG=$_val; fi
        ;;
    --since)
        case $_val in
        '' | *[!0-9]* | 0) die "--since takes a whole number of days" ;;
        esac
        SINCE=$_val
        SINCE_SET=1
        ;;
    esac
done

HELM_REQ=0
[ "$MODE_ARG" = helm ] && HELM_REQ=1
if [ "$HELM_REQ" = 0 ] && { [ -n "$REL_ARG" ] || [ -n "$NS_ARG" ]; }; then
    die "--release and --namespace need --mode helm"
fi
[ "$HELM_REQ" = 1 ] && [ -n "$STATE_ARG" ] && die "--state-dir does not apply to --mode helm"

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
        ("$@" 2>"$_stem.err" </dev/null; echo $? >"$_stem.st") | tee "$_stem.fifo" | tail -c "$SRC_MAX" >"$_stem.out"
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

# Exact secret values, for the filter's -v values=. The filter itself says which
# values to learn (credential-class fields and URL passwords, not comments or
# usernames), so the same value is also caught where it shows up in free text
# (container logs, the process list).
LEARN=0
learn() { # file
    [ -s "$1" ] || return 0
    _lr=$WORK/learn
    tr -d '\000' <"$1" >"$_lr.in"
    if awk -f "$REDACT" -v names="$NAMES" -v learn=1 <"$_lr.in" >"$_lr.out" 2>/dev/null; then
        cat "$_lr.out" >>"$VALUES"
    fi
    rm -f "$_lr.in" "$_lr.out"
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

# What went wrong with the run_limited run just finished (STEM, RC); nothing when
# it went well. ELEV 1: a refusal means "needs elevation"; 2: so does kubectl's
# or helm's "forbidden", with the cluster's message naming the missing permission.
# ABSENT_RE / ABSENT_WHY: a failure matching this is an expected absence.
failure_reason() { # stem elev limit
    if [ "$RC" = 124 ]; then
        printf '%s\n' "timed out after ${3} s"
        return
    fi
    case $2 in
    1)
        # journalctl says so with exit 0, so a clean exit only counts for its message
        _re=$ELEV_RE
        [ "$RC" = 0 ] && _re='insufficient permissions'
        if { cat "$1.err"; [ "$(wc -c <"$1.out")" -lt 2048 ] && cat "$1.out"; } 2>/dev/null | grep -qiE "$_re"; then
            printf '%s\n' "needs elevation"
            return
        fi
        ;;
    2)
        if [ "$RC" != 0 ] && grep -qi forbidden "$1.err" 2>/dev/null; then
            _e=$(err_line "$1")
            printf '%s\n' "needs elevation${_e:+: $_e}"
            return
        fi
        ;;
    esac
    [ "$RC" = 0 ] && return
    if [ -n "$ABSENT_RE" ] && grep -qiE "$ABSENT_RE" "$1.err" 2>/dev/null; then
        printf '%s\n' "$ABSENT_WHY"
        return
    fi
    _e=$(err_line "$1")
    printf '%s\n' "exit status $RC${_e:+: $_e}"
}

ABSENT_RE=
ABSENT_WHY=

# collect_cmd DEST SECS SOURCE ELEV cmd args...   (ELEV: see failure_reason)
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
    _why=$(failure_reason "$_stem" "$_elev" "$_lim")
    if [ -n "$_why" ]; then
        skip "$_dest" "$_why" "$_src"
    else
        [ "$LEARN" = 1 ] && learn "$_stem.out"
        store "$_dest" "$_stem" "$_src"
    fi
    EXTRA_CUT=0
    ABSENT_RE=
    ABSENT_WHY=
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

# Helm is chosen with --mode helm only, never probed: a stray kubeconfig on a
# Docker host would otherwise be contacted. HELM_RELS lists the Vigil releases as
# name, namespace, chart and app version, tab separated.
HELM_RELS=$WORK/helm-releases.txt
HELM_WHY=
HELM_TOOL_WHY=
detect_helm() {
    : >"$HELM_RELS"
    for _t in helm kubectl; do
        if ! has "$_t"; then
            HELM_WHY="$_t not found"
            HELM_TOOL_WHY=$HELM_WHY
            echo "$_t: not found; no release looked for" >>"$LOOKED"
            return
        fi
    done
    if [ -n "$NS_ARG" ]; then _scope="-n $NS_ARG"; else _scope=-A; fi
    ITEM=$((ITEM + 1))
    # shellcheck disable=SC2086
    run_limited 30 "$RAW/$ITEM" helm list -o json $_scope
    if [ "$RC" != 0 ]; then
        _e=$(err_line "$RAW/$ITEM")
        HELM_WHY="helm list failed (exit $RC)"
        if grep -qi forbidden "$RAW/$ITEM.err" 2>/dev/null; then
            HELM_WHY="needs elevation${_e:+: $_e} (try --namespace NS)"
        fi
        printf 'helm list %s: failed (exit %s)%s\n' "$_scope" "$RC" "${_e:+: $_e}" >>"$LOOKED"
        return
    fi
    # One JSON object per line; only names that are safe in a command line and a path.
    tr '{}' '\n\n' <"$RAW/$ITEM.out" | awk -F '\t' '
        function f(k,   m) {
            if (!match($0, "\"" k "\" *: *\"[^\"]*\"")) return ""
            m = substr($0, RSTART, RLENGTH)
            sub(/^"[^"]*" *: *"/, "", m); sub(/"$/, "", m)
            return m
        }
        f("name") ~ /^[A-Za-z0-9._-]+$/ && f("namespace") ~ /^[A-Za-z0-9._-]+$/ {
            print f("name") "\t" f("namespace") "\t" f("chart") "\t" f("app_version")
        }' >"$WORK/helm-all.txt"
    awk -F '\t' -v rel="$REL_ARG" '$3 ~ /^vigil-/ && (rel == "" || $1 == rel)' "$WORK/helm-all.txt" >"$HELM_RELS"
    _all=$(wc -l <"$WORK/helm-all.txt" | tr -d ' ')
    _v=$(wc -l <"$HELM_RELS" | tr -d ' ')
    printf 'helm list %s: %s releases returned, %s matching a vigil-* chart%s\n' "$_scope" "$_all" "$_v" \
        "${REL_ARG:+ named $REL_ARG}" >>"$LOOKED"
    while IFS='	' read -r _n _ns _ch _av; do
        printf 'helm\tHelm release %s, namespace %s, chart %s, app version %s\n' "$_n" "$_ns" "$_ch" "${_av:-unknown}" >>"$INSTALLS"
    done <"$HELM_RELS"
}

detect() {
    echo "helm: not probed, use --mode helm" >>"$LOOKED"
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
if [ "$HELM_REQ" = 1 ]; then detect_helm; else detect; fi
N_INSTALLS=$(wc -l <"$INSTALLS" | tr -d ' ')
if [ "$N_INSTALLS" -gt 1 ]; then
    echo
    echo "More than one Vigil install was found, so nothing was written:"
    sed 's/^[^	]*	/  - /' "$INSTALLS"
    if [ "$HELM_REQ" = 1 ]; then
        echo "Pick one with --release NAME --namespace NS and run again."
    else
        echo "Pick one with --mode native|compose|desktop and run again."
    fi
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

HELM_REL=
HELM_NS=
[ "$MODE" = helm ] && IFS='	' read -r HELM_REL HELM_NS _ <"$HELM_RELS"

if [ "$HELM_REQ" = 1 ]; then
    STATE_DIR=
elif [ -n "$STATE_ARG" ]; then
    STATE_DIR=$STATE_ARG
elif [ "$MODE" = desktop ]; then
    case $OS_KIND in
    Darwin) STATE_DIR=${HOME:-}/Library/Application\ Support/Vigil ;;
    *) STATE_DIR=${HOME:-}/.config/Vigil ;;
    esac
else
    STATE_DIR=${VIGIL_DIR:-${HOME:-}/.vigil}
fi
if [ "$HELM_REQ" = 0 ]; then
    if [ -d "$STATE_DIR" ]; then _found=found; else _found=none; fi
    printf "state dir: %s at %s\n" "$_found" "$STATE_DIR" >>"$LOOKED"
fi

NAME=vigil-support-$KIND-$VERSION-$STAMP
STAGE=$WORK/bundle/$NAME
mkdir -p "$STAGE/configuration" "$STAGE/health" "$STAGE/logs" "$STAGE/system"

# The API is asked for its version only; its payload is per-install collection.
# Helm takes it from the forwarded health answer instead: localhost may be some
# other install.
HEALTH_VERSION=
health_version() { # file with the /api/health answer
    HEALTH_VERSION=$(awk 'match($0, /"version" *: *"[^"]*"/) {
        v = substr($0, RSTART, RLENGTH); sub(/^"version" *: *"/, "", v); sub(/"$/, "", v); print v; exit }' "$1")
    case $HEALTH_VERSION in *[!0-9A-Za-z.+_-]*) HEALTH_VERSION= ;; esac # it lands in the manifest
}
if [ "$HELM_REQ" = 0 ] && [ "$N_INSTALLS" = 1 ] && has curl; then
    ITEM=$((ITEM + 1))
    run_limited 10 "$RAW/$ITEM" curl -fsS --max-time 5 "$API_URL/api/health"
    [ "$RC" = 0 ] && health_version "$RAW/$ITEM.out"
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
BACKEND= # the backend container of a Compose or Desktop install
list_containers() {
    : >"$KEPT"
    : >"$EXCLUDED"
    BACKEND=
    if ! has docker; then
        CONT_ERR="docker not found"
        return
    fi
    ITEM=$((ITEM + 1))
    run_limited 20 "$RAW/$ITEM" docker ps -a --format '{{.Names}}|{{.Label "com.docker.compose.project"}}|{{.State}}|{{.Label "com.docker.compose.service"}}'
    if [ "$RC" != 0 ]; then
        CONT_ERR="docker ps -a failed (exit $RC)"
        return
    fi
    while IFS='|' read -r _name _proj _state _svc; do
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
        # Desktop's backend has no fixed name; its Compose service label does
        if [ -z "$BACKEND" ] && { [ "$_svc" = backend ] || [ "$_name" = deeptempo-backend ]; }; then BACKEND=$_name; fi
    done <"$RAW/$ITEM.out"
}

# Under Compose and Desktop the State Directory is a volume of the backend
# container, so it is read with `docker cp` (a TAR stream; works on a stopped
# container and runs nothing in it). --state-dir makes the host copy win.
state_in_container() { [ "$MODE" != native ] && [ -z "$STATE_ARG" ]; }

STATE_MOUNT=
STATE_WHY=
resolve_state_mount() {
    STATE_MOUNT=
    STATE_WHY=
    state_in_container || return 0
    if [ -z "$BACKEND" ]; then
        STATE_WHY="no backend container of this install${CONT_ERR:+ ($CONT_ERR)}"
        return 0
    fi
    ITEM=$((ITEM + 1))
    # --format on .Mounts only: never raw inspect, never the container environment
    run_limited 20 "$RAW/$ITEM" docker inspect --format '{{range .Mounts}}{{.Destination}}{{"\n"}}{{end}}' "$BACKEND"
    if [ "$RC" != 0 ]; then
        STATE_WHY="docker inspect of $BACKEND failed (exit $RC)"
        return 0
    fi
    STATE_MOUNT=$(awk '/\/\.vigil$/ { print; exit }' "$RAW/$ITEM.out")
    case $STATE_MOUNT in
    '' | *[!A-Za-z0-9_./-]* | [!/]*)
        STATE_MOUNT=
        STATE_WHY="no State Directory mount (a path ending in /.vigil) in $BACKEND"
        ;;
    esac
}

# docker cp into a temp tar, then extract the one regular file by name to stdout.
# A symlink, a directory or any other member fails, so nothing is followed.
CP_MEMBER='t=$3; b=${2##*/}
docker cp "$1:$2" - >"$t" || { rm -f "$t"; exit 1; }
n=$(tar -tf "$t" 2>/dev/null); k=$(tar -tvf "$t" 2>/dev/null | cut -c1)
if [ "$n" != "$b" ] || [ "$k" != - ]; then rm -f "$t"; echo "not a regular file in the container" >&2; exit 1; fi
tar -xOf "$t" "$b"; r=$?; rm -f "$t"; exit $r'

# collect_state DEST NAME [HOSTDIR...]   a State Directory file, from the host or the backend container
collect_state() {
    if ! state_in_container; then
        _sd=$1
        _sn=$2
        shift 2
        collect_where "$_sd" "$_sn" "$STATE_DIR" "$@"
    elif [ -n "$STATE_WHY" ]; then
        skip "$1" "$STATE_WHY" "State Directory in the backend container"
    else
        collect_cmd "$1" "$SRC_SECS" "docker cp $BACKEND:$STATE_MOUNT/$2" 0 \
            sh -c "$CP_MEMBER" _ "$BACKEND" "$STATE_MOUNT/$2" "$WORK/cp.tar"
    fi
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
    set --
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
    collect_state configuration/state/backups.json backups.json "$_ck"
    collect_state configuration/state/detection_sources.json detection_sources.json
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
    # Repositories of a container's State Directory are paths in the container
    # (not checked); they are read from the redacted copy already in the bundle.
    _bj=$STATE_DIR/backups.json
    state_in_container && _bj=$STAGE/configuration/state/backups.json
    if [ -r "$_bj" ]; then
        awk '{ s = $0; while (match(s, /"repo" *: *"[^"]*"/)) {
            v = substr(s, RSTART, RLENGTH); sub(/^"repo" *: *"/, "", v); sub(/"$/, "", v); print v
            s = substr(s, RSTART + RLENGTH) } }' "$_bj" | head -n 20 >"$WORK/repos.txt"
        _i=0
        while IFS= read -r _r; do
            _i=$((_i + 1))
            case $_r in
            /*)
                if state_in_container; then
                    never "backup-repository-$_i" "$_r" "not checked: State Directory is in a container"
                else
                    never "backup-repository-$_i" "$_r"
                fi
                ;;
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
        ;;
    desktop)
        if [ "$OS_KIND" = Darwin ] && [ -z "$STATE_ARG" ]; then _dl=${HOME:-}/Library/Logs/Vigil; else _dl=$STATE_DIR/logs; fi
        collect_tree logs/desktop "$_dl" \( -name 'vigil-desktop.log*' -o \( -path '*/containers/*' -name 'vigil-*.log' \) \)
        ;;
    esac
    collect_state logs/state/vigil.log vigil.log
    if [ -n "$CONT_ERR" ]; then
        skip logs/docker/ "$CONT_ERR"
    else
        while IFS= read -r _n; do
            collect_cmd "logs/docker/$_n.log" "$LOG_SECS" "docker logs --since ${SINCE}d" 0 sh -c \
                'exec docker logs --timestamps --since "$1" "$2" 2>&1' _ "$((SINCE * 24))h" "$_n"
        done <"$KEPT"
    fi
    if [ ! -s "$KEPT" ] && [ -z "$CONT_ERR" ]; then
        skip logs/docker/ "no containers of this install"
    fi
}

# --- Helm --------------------------------------------------------------------

# Everything is read with the administrator's own kubectl and helm, from the
# release's namespace. Nothing runs inside the cluster beyond a port-forward.
HELM_HOST_WHY="Helm: the host is not the install"
HELM_LAB='pgadmin|splunk'

# kube_list DEST SOURCE cmd...   cmd prints names; LIST is its output. On failure
# DEST is recorded as not collected and the status is 1.
LIST=
kube_list() {
    _ld=$1
    _ls=$2
    shift 2
    ITEM=$((ITEM + 1))
    _stem=$RAW/$ITEM
    LIST=$_stem.out
    run_limited 30 "$_stem" "$@"
    _why=$(failure_reason "$_stem" 2 30)
    [ -z "$_why" ] && return 0
    skip "$_ld" "$_why" "$_ls"
    return 1
}

# GET /api/health through a port-forward to the backend Service, on a local port
# kubectl picks. The forward is a child of this function's job, so the time limit
# and the exit traps take it down with everything else.
api_via_forward() { # service
    has curl || { echo "curl not found" >&2; return 1; }
    _pf=$WORK/port-forward.out
    : >"$_pf"
    kubectl -n "$HELM_NS" port-forward "svc/$1" :6987 >"$_pf" 2>&1 &
    _pid=$!
    _port=
    while :; do
        _port=$(sed -n 's/^Forwarding from 127\.0\.0\.1:\([0-9][0-9]*\) .*/\1/p' "$_pf" | head -n 1)
        [ -n "$_port" ] && break
        if ! kill -0 "$_pid" 2>/dev/null; then
            cat "$_pf" >&2
            return 1
        fi
        sleep 1
    done
    curl -fsS --noproxy '*' --max-time 5 "http://127.0.0.1:$_port/api/health"
    _rc=$?
    kill_tree "$_pid" KILL
    return $_rc
}

# Node names from the NODE column of `get pods -o wide`: the column is cut at its
# header's offsets, since a cell such as "1 (5m ago)" holds spaces.
pod_nodes() { # kubectl get pods args
    kubectl "$@" -o wide >"$WORK/pods-wide.txt" || return $?
    awk 'NR == 1 { s = index($0, "NODE"); e = index($0, "NOMINATED"); next }
        s { v = (e > s ? substr($0, s, e - s) : substr($0, s)); gsub(/^ +| +$/, "", v); if (v != "" && v != "<none>") print v }' \
        "$WORK/pods-wide.txt" | sort -u
}

tools_info() {
    kubectl version --client 2>&1
    helm version 2>&1
}

helm_configuration() {
    _ns=$1
    _sel=$2
    # Values first, so what they hold is known before anything else is stored.
    collect_secret_cmd configuration/helm-values.yaml "$SRC_SECS" "helm get values $HELM_REL" 2 \
        helm get values "$HELM_REL" -n "$_ns" -o yaml
    collect_secret_cmd configuration/helm-values-all.yaml "$SRC_SECS" "helm get values --all $HELM_REL" 2 \
        helm get values "$HELM_REL" -n "$_ns" --all -o yaml
    # Names and key names only; the template never prints a value.
    collect_cmd configuration/kubernetes-secrets.txt "$SRC_SECS" "kubectl get secrets, names and keys only" 2 \
        kubectl -n "$_ns" get secrets -o \
        'go-template={{range .items}}{{.metadata.name}} {{.type}}{{range $k, $v := .data}} {{$k}}{{end}}{{"\n"}}{{end}}'
    if kube_list configuration/configmaps/ "kubectl get configmaps" \
        kubectl -n "$_ns" get configmaps -l "$_sel" --no-headers -o custom-columns=NAME:.metadata.name; then
        cp "$LIST" "$WORK/configmaps.list"
        [ -s "$WORK/configmaps.list" ] || skip configuration/configmaps/ "no ConfigMaps of this release" "kubectl get configmaps"
        while IFS= read -r _cm; do
            case $_cm in '' | *[!A-Za-z0-9_.-]*) continue ;; esac
            collect_cmd "configuration/configmaps/$_cm.yaml" "$SRC_SECS" "kubectl get configmap $_cm -o yaml" 2 \
                kubectl -n "$_ns" get configmap "$_cm" -o yaml
        done <"$WORK/configmaps.list"
    fi
}

helm_health() {
    _ns=$1
    _sel=$2
    collect_cmd health/pods.txt "$SRC_SECS" "kubectl get pods -o wide" 2 kubectl -n "$_ns" get pods -l "$_sel" -o wide
    collect_cmd health/workloads.txt "$SRC_SECS" "kubectl get deploy,statefulset,job,hpa" 2 \
        kubectl -n "$_ns" get deploy,statefulset,job,hpa -l "$_sel"
    collect_cmd health/pods-describe.txt "$SRC_SECS" "kubectl describe pods" 2 kubectl -n "$_ns" describe pods -l "$_sel"
    for _f in daemon-health.json daemon-status.json webhook-health.json agent-serve-healthz.txt \
        agent-worker-healthz.txt bifrost-health.json; do
        skip "health/$_f" "Helm: only /api/health is read; see health/pods.txt"
    done
    if kube_list health/api.json "kubectl get service" \
        kubectl -n "$_ns" get service -l "$_sel,app.kubernetes.io/component=backend" --no-headers -o custom-columns=NAME:.metadata.name; then
        _svc=$(sed -n 1p "$LIST")
        case $_svc in
        '' | *[!A-Za-z0-9_.-]*) skip health/api.json "no backend Service in this release" "kubectl get service" ;;
        *)
            _api_item=$((ITEM + 1))
            collect_cmd health/api.json "$SRC_SECS" "kubectl port-forward svc/$_svc, GET /api/health" 2 api_via_forward "$_svc"
            health_version "$RAW/$_api_item.out"
            ;;
        esac
    fi
}

helm_logs() {
    _ns=$1
    # Lab pods are listed too, to be recorded as excluded.
    if ! kube_list logs/pods/ "kubectl get pods" kubectl -n "$_ns" get pods -l "app.kubernetes.io/instance=$HELM_REL" \
        --no-headers -o 'custom-columns=NAME:.metadata.name,COMPONENT:.metadata.labels.app\.kubernetes\.io/component,PHASE:.status.phase,INIT:.spec.initContainers[*].name,CONTAINERS:.spec.containers[*].name'; then
        return
    fi
    cp "$LIST" "$WORK/pods.list"
    if [ ! -s "$WORK/pods.list" ]; then
        skip logs/pods/ "no pods of this release" "kubectl get pods"
        return
    fi
    skip logs/pods/ "only pods that exist now can be read; a pod that was replaced or rescheduled needs the cluster's own log collection" "kubectl get pods"
    while read -r _pod _comp _phase _init _conts; do
        case $_pod in '' | *[!A-Za-z0-9_.-]*) continue ;; esac
        case $_comp in
        pgadmin | splunk)
            skip "logs/pods/$_pod/" "excluded: lab/demo component $_comp; state ${_phase:-unknown}" "kubectl get pods"
            continue
            ;;
        esac
        _oifs=$IFS
        IFS=,
        # shellcheck disable=SC2086
        set -- $_init $_conts
        IFS=$_oifs
        for _ctr in "$@"; do
            case $_ctr in '<none>' | '' | *[!A-Za-z0-9_.-]*) continue ;; esac
            collect_cmd "logs/pods/$_pod/$_ctr.log" "$LOG_SECS" "kubectl logs --since ${SINCE}d" 2 \
                kubectl -n "$_ns" logs "$_pod" -c "$_ctr" --timestamps --since "$((SINCE * 24))h"
            ABSENT_RE='previous terminated container'
            ABSENT_WHY="no previous container"
            collect_cmd "logs/pods/$_pod/$_ctr.previous.log" "$LOG_SECS" "kubectl logs --previous" 2 \
                kubectl -n "$_ns" logs "$_pod" -c "$_ctr" --previous --timestamps --since "$((SINCE * 24))h"
        done
    done <"$WORK/pods.list"
    # In-cluster Postgres and Redis show up as pods; their absence means external.
    for _x in postgres redis; do
        grep -qi "$_x" "$WORK/pods.list" ||
            skip "logs/pods/$_x/" "external, not applicable: no $_x pod in this release" "kubectl get pods"
    done
}

collect_helm() {
    if [ "$N_INSTALLS" != 1 ]; then
        for _p in configuration health logs; do skip "$_p/" "${HELM_WHY:-no Vigil install found}"; done
        return
    fi
    _sel="app.kubernetes.io/instance=$HELM_REL,app.kubernetes.io/component notin ($(echo "$HELM_LAB" | tr '|' ','))"
    helm_configuration "$HELM_NS" "$_sel"
    helm_health "$HELM_NS" "$_sel"
    helm_logs "$HELM_NS"
}

collect_helm_system() {
    if [ -n "$HELM_TOOL_WHY" ]; then
        skip system/tools.txt "$HELM_TOOL_WHY"
    else
        collect_cmd system/tools.txt "$SRC_SECS" "kubectl version --client; helm version" 0 tools_info
    fi
    if [ "$N_INSTALLS" = 1 ]; then
        collect_cmd system/nodes.txt "$SRC_SECS" "kubectl get pods -o wide, NODE column" 2 \
            pod_nodes -n "$HELM_NS" get pods -l "app.kubernetes.io/instance=$HELM_REL"
        collect_cmd system/events.txt "$SRC_SECS" "kubectl get events" 2 \
            kubectl -n "$HELM_NS" get events --sort-by=.lastTimestamp
    else
        for _f in nodes events; do skip "system/$_f.txt" "${HELM_WHY:-no Vigil install found}"; done
    fi
    for _f in timezone-sync processes processes-vigil disk journal syslog kernel macos-log; do
        skip "system/$_f.txt" "$HELM_HOST_WHY"
    done
}

section 2/6 "collecting configuration, health and logs"
if [ "$HELM_REQ" = 1 ]; then
    collect_helm
elif [ "$N_INSTALLS" = 1 ]; then
    list_containers
    find_compose_files
    resolve_state_mount
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
if [ "$HELM_REQ" = 1 ]; then
    : # skipped with the other host-level files, below
elif has timedatectl; then
    collect_cmd system/timezone-sync.txt "$SRC_SECS" "timedatectl" 0 timedatectl
elif has systemsetup; then
    collect_cmd system/timezone-sync.txt "$SRC_SECS" "systemsetup" 0 sh -c 'systemsetup -gettimezone; systemsetup -getusingnetworktime'
else
    skip system/timezone-sync.txt "neither timedatectl nor systemsetup found"
fi
if [ "$HELM_REQ" = 0 ]; then
    collect_cmd system/processes.txt "$SRC_SECS" "ps -ef" 0 ps -ef
    collect_cmd system/processes-vigil.txt "$SRC_SECS" "ps -ef, Vigil entries" 0 \
        sh -c "ps -ef | awk 'NR == 1 || /[v]igil|[d]eeptempo/'"
fi

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
[ "$HELM_REQ" = 0 ] && collect_disk
if [ -r "$FS_ROOT/etc/os-release" ] || [ -L "$FS_ROOT/etc/os-release" ]; then
    collect_file system/os-release.txt "$FS_ROOT/etc/os-release" 0 "$FS_ROOT/etc" "$FS_ROOT/usr/lib"
elif has sw_vers; then
    collect_cmd system/os-release.txt "$SRC_SECS" "sw_vers" 0 sw_vers
else
    skip system/os-release.txt "no /etc/os-release and no sw_vers"
fi

section 5/6 "system logs"
if [ "$HELM_REQ" = 1 ]; then
    collect_helm_system
elif [ "$OS_KIND" = Darwin ]; then
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
    # never-included entries are existence-only by design, not failures
    awk -F '\t' '$2 == "collected" { next }
        index($3, "never included;") == 1 { v++; vl = vl "  " $1 ": " $3 "\n"; next }
        { n++; l = l "  " $1 ": " $3 "\n" }
        END { printf "Not collected (%d)\n%s", n, l
            if (v) printf "\nNever included (existence only)\n%s", vl }' "$ITEMS"
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
if awk -F '\t' '$2 != "collected" && index($3, "never included;") != 1 { f = 1 } END { exit !f }' "$ITEMS"; then
    echo
    echo "Not collected:"
    awk -F '\t' '$2 != "collected" && index($3, "never included;") != 1 { print "  " $1 ": " $3 }' "$ITEMS"
    if awk -F '\t' '$3 == "needs elevation" { f = 1 } END { exit !f }' "$ITEMS"; then
        echo "Run with sudo to include the items marked \"needs elevation\"."
    fi
    if awk -F '\t' '$2 != "collected" && $3 ~ /^needs elevation: / { f = 1 } END { exit !f }' "$ITEMS"; then
        echo "Cluster permissions are missing for the items marked \"needs elevation\"; each reason names the permission the cluster refused."
    fi
fi
if awk -F '\t' 'index($3, "never included;") == 1 { f = 1 } END { exit !f }' "$ITEMS"; then
    echo
    echo "Never included (existence only):"
    awk -F '\t' 'index($3, "never included;") == 1 { print "  " $1 ": " $3 }' "$ITEMS"
fi
exit 0
