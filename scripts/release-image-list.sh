#!/usr/bin/env bash
# scripts/release-image-list.sh — write list.json: every image an install runs, by digest.
#
#   scripts/release-image-list.sh <version> <chart> <digests.json> [out=list.json]
#
#   <chart>         the chart to read, packaged (.tgz) or a directory with its
#                   dependencies built. Every Vigil image it names must carry
#                   <version> as its tag, so pass the chart packaged for <version>.
#   <digests.json>  {"vigil-backend": "sha256:…", …}: the index digest each Vigil
#                   package was pushed with (build-images.yml's outputs).
#
# Reads the images from the chart (`helm template`, defaults plus medic.enabled)
# and from Compose (the default set plus the daemon and medic profiles). Never a
# hand-written list. A Compose service with `build:` is a Vigil image, named for
# its Dockerfile (Dockerfile.<x> → vigil-<x>); a service with only `image:` is
# Vigil if another service builds that image, third-party otherwise. A chart ref
# under $VIGIL_REGISTRY is Vigil. Vigil refs take their digest from
# <digests.json>; third-party tags are resolved in their registry. Each image
# also lists its platform manifests, read from the registry.
#
# Fails if a Vigil image has no digest, a chart ref has the wrong tag, or any
# digest or tag does not resolve.
#
# Env: VIGIL_REGISTRY (default ghcr.io/vigil-soc): where Vigil packages live.
#      Its host is also passed to the chart as global.imageRegistry.
#      COMMIT (default: git rev-parse HEAD).
# Needs: helm, docker (compose + buildx), jq, python3 with PyYAML.

set -euo pipefail

[ $# -ge 3 ] || { sed -n '2,12p' "$0" >&2; exit 2; }
version=$1 chart=$2 digests=$3 out=${4:-list.json}
registry=${VIGIL_REGISTRY:-ghcr.io/vigil-soc}
root=$(cd "$(dirname "$0")/.." && pwd)
commit=${COMMIT:-$(git -C "$root" rev-parse HEAD)}
jq -e 'type == "object"' "$digests" >/dev/null || { echo "error: $digests is not a JSON object" >&2; exit 1; }

tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
fail=0
err() { echo "error: $*" >&2; fail=1; }

# --- Chart: the image of every container in every rendered pod spec ----------
helm template vigil "$chart" \
  --set global.imageRegistry="${registry%%/*}" \
  --set secrets.postgresPassword=list-owner,secrets.vigilAppPassword=list-app \
  --set medic.enabled=true \
  --set medic.gateway.viewer.username=list-viewer \
  --set 'medic.kubeApi.cidrs={10.0.0.1/32}' > "$tmp/chart.yaml"
python3 -I -c '
import sys, yaml
def walk(n):
    if isinstance(n, dict):
        for k, v in n.items():
            if k in ("containers", "initContainers", "ephemeralContainers") and isinstance(v, list):
                for c in v:
                    if isinstance(c, dict) and c.get("image"): print(c["image"])
            walk(v)
    elif isinstance(n, list):
        for v in n: walk(v)
for d in yaml.safe_load_all(open(sys.argv[1])): walk(d)
' "$tmp/chart.yaml" | sort -u > "$tmp/chart-refs"

# Lines of "<package or empty> <ref> <source>"; an empty package means third-party.
while read -r ref; do
  if [[ $ref == "$registry"/* ]]; then
    name=${ref#"$registry"/}
    [ "${name#*:}" = "$version" ] || err "chart names $ref, not tag $version (package the chart for this version)"
    echo "${name%%[:@]*} $ref helm"
  else
    echo "- $ref helm"
  fi
done < "$tmp/chart-refs" > "$tmp/refs"

# --- Compose: classify each service by structure, not by name ----------------
( cd "$root" && POSTGRES_PASSWORD=list POSTGRES_APP_PASSWORD=list REDIS_PASSWORD=list \
    docker compose -f infra/docker/docker-compose.yml -f infra/docker/medic/docker-compose.medic.yml \
      --profile daemon --profile medic config --format json ) > "$tmp/compose.json" 2> "$tmp/compose.err" \
  || { grep -v 'variable is not set' "$tmp/compose.err" >&2; exit 1; }
jq -r '
  .services as $s
  # Image names some service builds, and the Dockerfile that builds each.
  | ([$s[] | select(.build and .image) | {(.image): .build.dockerfile}] | add // {}) as $built
  | $s | to_entries[] | .key as $svc | .value
  | if .build then "df \(.build.dockerfile) \($svc)"
    elif .image and $built[.image] then "df \($built[.image]) \($svc)"
    else "img \(.image) \($svc)" end' "$tmp/compose.json" |
while read -r kind what svc; do
  if [ "$kind" = df ]; then
    base=$(basename "$what")
    [[ $base == Dockerfile.* ]] || { echo "error: compose service $svc builds $what, not a Dockerfile.<name>" >&2; echo "! $svc compose"; continue; }
    pkg=vigil-${base#Dockerfile.}
    echo "$pkg $registry/$pkg:$version compose"
  else
    echo "- $what compose"
  fi
done >> "$tmp/refs"
grep -q '^! ' "$tmp/refs" && fail=1

# --- Resolve every image -------------------------------------------------------
# inspect <ref>: {"digest", "platforms": [{platform, digest}]}. An index lists its
# platform manifests, without attestations; a single manifest is its own platform.
inspect() {
  local m
  m=$(docker buildx imagetools inspect --format '{{json .Manifest}}' "$1") || return 1
  if jq -e '.manifests' <<< "$m" >/dev/null; then
    jq -c '{digest, platforms: [.manifests[] | select(.platform.os != "unknown")
            | {platform: ([.platform.os, .platform.architecture, .platform.variant] | map(select(.)) | join("/")), digest}]}' <<< "$m"
  else
    local p
    p=$(docker buildx imagetools inspect --format '{{json .Image}}' "$1" | jq -r '[.os, .architecture, .variant] | map(select(.)) | join("/")') || return 1
    jq -c --arg p "$p" '{digest, platforms: [{platform: $p, digest}]}' <<< "$m"
  fi
}

: > "$tmp/images"
while read -r pkg ref; do
  sources=$(awk -v p="$pkg" -v r="$ref" '$1 == p && $2 == r {print $3}' "$tmp/refs" | sort -u | jq -Rsc 'split("\n") | map(select(. != ""))')
  if [ "$pkg" = - ]; then
    m=$(inspect "$ref") || { err "could not resolve $ref"; continue; }
    jq -c --arg ref "$ref" --argjson src "$sources" \
      '{ref: "\($ref)@\(.digest)", name: $ref, vigil: false, digest, platforms, sources: $src}' <<< "$m" >> "$tmp/images"
  else
    digest=$(jq -r --arg p "$pkg" '.[$p] // empty' "$digests")
    [ -n "$digest" ] || { err "no build digest for Vigil image $pkg ($ref)"; continue; }
    m=$(inspect "$registry/$pkg@$digest") || { err "could not read $registry/$pkg@$digest"; continue; }
    jq -c --arg ref "$ref" --arg d "$digest" --argjson src "$sources" \
      '{ref: "\($ref)@\($d)", name: $ref, vigil: true, digest: $d, platforms, sources: $src}' <<< "$m" >> "$tmp/images"
  fi
done < <(awk '$1 != "!" {print $1, $2}' "$tmp/refs" | sort -u)

[ "$fail" = 0 ] || { echo "error: list not written" >&2; exit 1; }
jq -s --arg v "$version" --arg c "$commit" \
  '{version: $v, commit: $c, images: (sort_by((.vigil | not), .name))}' "$tmp/images" > "$out"
echo "wrote $out: $(jq '.images | length' "$out") images ($(jq '[.images[] | select(.vigil)] | length' "$out") Vigil)" >&2
