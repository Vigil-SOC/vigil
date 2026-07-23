#!/usr/bin/env bash
# Build the backend image from the current source tree and stage it as an
# offline tarball for a no-registry-access DMG/AppImage. main.ts loads this on
# first run when present, so the artifact needs neither GHCR nor a network.
#
# Arch-specific: the tarball built here runs only on Docker hosts of the same
# arch. Build on (or --platform for) the target's arch, and bundle it only into
# the matching-arch artifact. Run before `npm run dist`.
set -euo pipefail

cd "$(dirname "$0")/../.."
VERSION="$(node -p "require('./desktop/package.json').version")"
IMAGE="ghcr.io/vigil-soc/vigil-backend:${VERSION}"
PLATFORM="${1:-linux/arm64}"
OUT="desktop/standalone/backend-image.tar.gz"

echo "building ${IMAGE} for ${PLATFORM}"
docker build --platform "${PLATFORM}" -f docker/Dockerfile.backend -t "${IMAGE}" .

echo "saving -> ${OUT}"
docker save "${IMAGE}" | gzip > "${OUT}"
echo "done: $(du -h "${OUT}" | cut -f1)"
