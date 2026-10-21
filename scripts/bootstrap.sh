#!/usr/bin/env bash
# ==============================================================================
# Vigil SOC bootstrap for a fresh Debian or Ubuntu host
# ==============================================================================
# Installs what ./start.sh expects on the host and then hands over to it:
# Docker CE with the Compose plugin (from Docker's APT repository, skipped when
# `docker compose` already works), Node.js 20+, and git. Clones Vigil unless the
# script is run from a checkout. ./start.sh creates .env, the JWT secret and the
# agent token itself.
#
# Usage:
#   ./scripts/bootstrap.sh
#   VIGIL_DIR=/opt/vigil ./scripts/bootstrap.sh
# ==============================================================================

set -euo pipefail

SCRIPT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
if [ -z "${VIGIL_DIR:-}" ]; then
  if [ -f "${SCRIPT_ROOT}/start.sh" ] && [ -d "${SCRIPT_ROOT}/.git" ]; then
    VIGIL_DIR="${SCRIPT_ROOT}"
  else
    VIGIL_DIR="${HOME}/vigil"
  fi
fi
: "${VIGIL_REPO_URL:=https://github.com/Vigil-SOC/vigil.git}"
: "${VIGIL_BRANCH:=main}"

if [ "$(id -u)" -eq 0 ]; then
  SUDO=""
else
  SUDO="sudo"
fi

echo "--> [1/6] Detecting operating system..."
if [ ! -f /etc/os-release ]; then
  echo "Error: Cannot detect operating system (/etc/os-release not found)." >&2
  exit 1
fi
# shellcheck disable=SC1091
. /etc/os-release

# Docker publishes one repository per release codename, so a derivative has to
# resolve to the Ubuntu or Debian release it is built on.
DOCKER_OS=""
CODENAME=""
case "${ID:-}" in
  debian)
    DOCKER_OS="debian"
    CODENAME="${VERSION_CODENAME:-}"
    ;;
  ubuntu)
    DOCKER_OS="ubuntu"
    CODENAME="${VERSION_CODENAME:-}"
    ;;
  *)
    if [[ "${ID_LIKE:-}" == *ubuntu* ]]; then
      DOCKER_OS="ubuntu"
      CODENAME="${UBUNTU_CODENAME:-}"
    fi
    ;;
esac
if [ -z "${DOCKER_OS}" ] || [ -z "${CODENAME}" ]; then
  echo "Error: Unsupported distribution '${ID:-unknown}'. Expected Debian or Ubuntu." >&2
  exit 1
fi
echo "    Detected: ${PRETTY_NAME:-${DOCKER_OS}} (Docker repo: ${DOCKER_OS}/${CODENAME})"

echo "--> [2/6] Installing base utilities..."
${SUDO} apt-get update
${SUDO} apt-get install -y ca-certificates curl gnupg git openssl
${SUDO} install -m 0755 -d /etc/apt/keyrings

NEED_NEWGRP=false
TARGET_USER="${SUDO_USER:-${USER:-$(id -un)}}"

if docker compose version >/dev/null 2>&1; then
  echo "--> [3/6] Docker with the Compose plugin is already installed; skipping."
else
  echo "--> [3/6] Installing Docker CE and the Compose plugin..."
  for pkg in docker.io docker-doc docker-compose docker-compose-v2 podman-docker containerd runc; do
    ${SUDO} apt-get remove -y "${pkg}" >/dev/null 2>&1 || true
  done

  ${SUDO} curl -fsSL "https://download.docker.com/linux/${DOCKER_OS}/gpg" -o /etc/apt/keyrings/docker.asc
  ${SUDO} chmod a+r /etc/apt/keyrings/docker.asc
  echo \
    "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/${DOCKER_OS} ${CODENAME} stable" | \
    ${SUDO} tee /etc/apt/sources.list.d/docker.list >/dev/null

  ${SUDO} apt-get update
  ${SUDO} apt-get install -y \
    docker-ce \
    docker-ce-cli \
    containerd.io \
    docker-buildx-plugin \
    docker-compose-plugin
  ${SUDO} systemctl enable --now docker

  if [ "${TARGET_USER}" != "root" ] && ! id -nG "${TARGET_USER}" | grep -qw docker; then
    ${SUDO} usermod -aG docker "${TARGET_USER}"
    NEED_NEWGRP=true
    echo "    Added user '${TARGET_USER}' to the 'docker' group."
  fi
fi

echo "--> [4/6] Checking Node.js 20+ and npm (the agent layer and frontend need them)..."
if ! command -v node >/dev/null 2>&1 \
  || ! command -v npm >/dev/null 2>&1 \
  || ! node -e "process.exit(parseInt(process.version.slice(1))>=20?0:1)" 2>/dev/null; then
  CANDIDATE_MAJOR="$(apt-cache policy nodejs 2>/dev/null | awk '/Candidate:/ { split($2, a, "."); gsub(/[^0-9]/, "", a[1]); print a[1] }')"
  if [ -n "${CANDIDATE_MAJOR}" ] && [ "${CANDIDATE_MAJOR}" -ge 20 ] 2>/dev/null; then
    ${SUDO} apt-get install -y nodejs npm
  else
    curl -fsSL https://deb.nodesource.com/gpgkey/nodesource-repo.gpg.key | \
      ${SUDO} gpg --dearmor --yes -o /etc/apt/keyrings/nodesource.gpg
    echo "deb [signed-by=/etc/apt/keyrings/nodesource.gpg] https://deb.nodesource.com/node_22.x nodistro main" | \
      ${SUDO} tee /etc/apt/sources.list.d/nodesource.list >/dev/null
    ${SUDO} apt-get update
    ${SUDO} apt-get install -y nodejs
  fi
fi

echo "--> [5/6] Getting Vigil into ${VIGIL_DIR}..."
if [ -d "${VIGIL_DIR}/.git" ]; then
  echo "    Existing repository found; leaving it as is."
else
  mkdir -p "$(dirname "${VIGIL_DIR}")"
  git clone --branch "${VIGIL_BRANCH}" "${VIGIL_REPO_URL}" "${VIGIL_DIR}"
fi

echo "--> [6/6] Verifying installation..."
docker --version
docker compose version
node --version
npm --version

echo ""
echo "=============================================================================="
echo " Vigil SOC bootstrap complete"
echo "=============================================================================="
echo ""
if [ "${NEED_NEWGRP}" = true ]; then
  echo " '${TARGET_USER}' was just added to the 'docker' group. Log out and back in"
  echo " (or run 'newgrp docker') so Docker works without sudo in your shell."
  echo ""
fi
echo " Start Vigil:"
echo ""
echo "   cd ${VIGIL_DIR}"
echo "   ./start.sh          # foreground; Ctrl+C to stop"
echo "   ./start.sh -d       # background, with the 24/7 SOC daemon"
echo ""
echo " Then open http://localhost:6988 and create the admin account."
echo " While no account exists, the API prints a setup code in the start.sh terminal; enter it there."
echo " Stop with ./shutdown_all.sh (add -d to stop the containers too)."
echo "=============================================================================="
