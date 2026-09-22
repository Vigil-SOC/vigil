#!/usr/bin/env bash
# Build the backend and agent images from the current source tree and stage
# them as one offline tarball for a no-registry-access DMG/AppImage. main.ts
# loads this on first run when present, so the artifact needs neither GHCR nor
# a network.
#
# Arch-specific: the tarball built here runs only on Docker hosts of the same
# arch. Build on (or --platform for) the target's arch, and bundle it only into
# the matching-arch artifact. Run before `npm run dist`.
set -euo pipefail

cd "$(dirname "$0")/../../.."
VERSION="$(node -p "require('./clients/desktop/package.json').version")"
BACKEND_IMAGE="ghcr.io/vigil-soc/vigil-backend:${VERSION}"
AGENT_IMAGE="ghcr.io/vigil-soc/vigil-agent:${VERSION}"
PLATFORM="${1:-linux/arm64}"
# Name kept for stageStandalone(), which skips it by name; it holds both images.
OUT="clients/desktop/standalone/backend-image.tar.gz"

echo "building ${BACKEND_IMAGE} for ${PLATFORM}"
docker build --platform "${PLATFORM}" -f infra/docker/Dockerfile.backend -t "${BACKEND_IMAGE}" .
echo "building ${AGENT_IMAGE} for ${PLATFORM}"
docker build --platform "${PLATFORM}" -f infra/docker/Dockerfile.agent -t "${AGENT_IMAGE}" .

echo "saving -> ${OUT}"
docker save "${BACKEND_IMAGE}" "${AGENT_IMAGE}" | gzip > "${OUT}"
echo "done: $(du -h "${OUT}" | cut -f1)"
