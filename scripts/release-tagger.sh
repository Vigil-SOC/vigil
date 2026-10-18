#!/usr/bin/env bash
# ==============================================================================
# Vigil Release & Version Tagger
# ==============================================================================
# Usage:
#   ./scripts/release-tagger.sh patch      # e.g. 0.7.0 -> 0.7.1
#   ./scripts/release-tagger.sh minor      # e.g. 0.7.0 -> 0.8.0
#   ./scripts/release-tagger.sh major      # e.g. 0.7.0 -> 1.0.0
#   ./scripts/release-tagger.sh 0.7.1      # Explicit version
#   ./scripts/release-tagger.sh --push     # Also push commits and tags to origin
# ==============================================================================

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${REPO_ROOT}"

VERSION_FILE="${REPO_ROOT}/VERSION"

if [ ! -f "${VERSION_FILE}" ]; then
  echo "Error: VERSION file not found at ${VERSION_FILE}" >&2
  exit 1
fi

CURRENT_VERSION=$(tr -d "[:space:]" < "${VERSION_FILE}")

TARGET_ARG="patch"
PUSH_FLAG=false

for arg in "$@"; do
  if [ "$arg" == "--push" ] || [ "$arg" == "-p" ]; then
    PUSH_FLAG=true
  else
    TARGET_ARG="$arg"
  fi
done

IFS="." read -r MAJOR MINOR PATCH <<< "${CURRENT_VERSION}"
PATCH="${PATCH%%-*}"

case "${TARGET_ARG}" in
  patch)
    NEW_PATCH=$((PATCH + 1))
    NEW_VERSION="${MAJOR}.${MINOR}.${NEW_PATCH}"
    ;;
  minor)
    NEW_MINOR=$((MINOR + 1))
    NEW_VERSION="${MAJOR}.${NEW_MINOR}.0"
    ;;
  major)
    NEW_MAJOR=$((MAJOR + 1))
    NEW_VERSION="${NEW_MAJOR}.0.0"
    ;;
  v*)
    NEW_VERSION="${TARGET_ARG#v}"
    ;;
  *)
    if [[ "${TARGET_ARG}" =~ ^[0-9]+\.[0-9]+\.[0-9]+.*$ ]]; then
      NEW_VERSION="${TARGET_ARG}"
    else
      echo "Error: Unknown argument \"${TARGET_ARG}\"."
      echo "Usage: $0 [patch|minor|major|<version>] [--push]"
      exit 1
    fi
    ;;
esac

TAG_NAME="v${NEW_VERSION}"

echo "============================================================"
echo " Vigil Release Tagger"
echo " Current Version: ${CURRENT_VERSION}"
echo " New Version:     ${NEW_VERSION} (${TAG_NAME})"
echo "============================================================"

if git rev-parse "${TAG_NAME}" >/dev/null 2>&1; then
  echo "Error: Tag ${TAG_NAME} already exists in git!"
  exit 1
fi

echo ">> Updating VERSION files..."
echo "${NEW_VERSION}" > "${VERSION_FILE}"

if [ -f scripts/vigil-support/VERSION ]; then
  echo "${NEW_VERSION} # x-release-please-version" > scripts/vigil-support/VERSION
fi

if [ -f .github/.release-please-manifest.json ]; then
  echo ">> Updating .github/.release-please-manifest.json..."
  node -e "
    const fs = require('fs');
    const p = '.github/.release-please-manifest.json';
    const json = JSON.parse(fs.readFileSync(p, 'utf8'));
    json['.'] = '${NEW_VERSION}';
    fs.writeFileSync(p, JSON.stringify(json, null, 2) + '\n');
  "
fi

echo ">> Updating package.json and package-lock.json files..."
for pkg in clients/web/package.json clients/desktop/package.json; do
  if [ -f "${pkg}" ]; then
    node -e "
      const fs = require('fs');
      const p = '${pkg}';
      const json = JSON.parse(fs.readFileSync(p, 'utf8'));
      json.version = '${NEW_VERSION}';
      fs.writeFileSync(p, JSON.stringify(json, null, 2) + '\n');
    "
  fi
done

for lock in clients/web/package-lock.json clients/desktop/package-lock.json; do
  if [ -f "${lock}" ]; then
    node -e "
      const fs = require('fs');
      const p = '${lock}';
      const json = JSON.parse(fs.readFileSync(p, 'utf8'));
      json.version = '${NEW_VERSION}';
      if (json.packages && json.packages['']) {
        json.packages[''].version = '${NEW_VERSION}';
      }
      fs.writeFileSync(p, JSON.stringify(json, null, 2) + '\n');
    "
  fi
done

echo ">> Updating Helm Chart.yaml..."
if [ -f infra/helm/vigil/Chart.yaml ]; then
  sed -i -E "s/^version: [0-9]+\.[0-9]+\.[0-9]+/version: ${NEW_VERSION}/" infra/helm/vigil/Chart.yaml
  sed -i -E "s/^appVersion: [0-9]+\.[0-9]+\.[0-9]+/appVersion: ${NEW_VERSION}/" infra/helm/vigil/Chart.yaml
fi

echo ">> Staging and creating git commit..."
git add VERSION \
        scripts/vigil-support/VERSION \
        .github/.release-please-manifest.json \
        clients/web/package.json \
        clients/web/package-lock.json \
        clients/desktop/package.json \
        clients/desktop/package-lock.json \
        infra/helm/vigil/Chart.yaml

git commit -m "chore(release): bump version to ${NEW_VERSION}"

echo ">> Creating git tag ${TAG_NAME}..."
git tag -a "${TAG_NAME}" -m "Release ${TAG_NAME}"

echo "============================================================"
echo " Successfully created release ${TAG_NAME}!"
echo " Commit: $(git rev-parse --short HEAD)"
echo " Tag:    ${TAG_NAME}"
echo "============================================================"

CURRENT_BRANCH="$(git rev-parse --abbrev-ref HEAD)"

if [ "${PUSH_FLAG}" = true ]; then
  echo ">> Pushing branch (${CURRENT_BRANCH}) and tag (${TAG_NAME}) to origin..."
  git push origin "${CURRENT_BRANCH}"
  git push origin "${TAG_NAME}"
  echo ">> Pushed! GitHub Actions workflow \"Release\" will now trigger on tag ${TAG_NAME}."
else
  echo ""
  echo "To trigger GitHub Actions build for this release, push branch and tag:"
  echo "  git push origin ${CURRENT_BRANCH}"
  echo "  git push origin ${TAG_NAME}"
  echo ""
  echo "Or re-run this script with --push flag next time:"
  echo "  ./scripts/release-tagger.sh [patch|minor|major] --push"
fi
