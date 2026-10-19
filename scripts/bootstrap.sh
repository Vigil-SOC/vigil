#!/usr/bin/env bash
# ==============================================================================
# Vigil SOC Bootstrap Script (Debian 13 / Ubuntu)
# ==============================================================================
# Installs Docker CE from Docker's official APT repository, ensures Node.js 20+
# is available for the frontend and TypeScript agent layer, clones the Vigil SOC
# repository, initializes the environment via scripts/lib.sh, and directs the
# operator to ./start.sh and ./shutdown_all.sh.
#
# Usage:
#   ./scripts/bootstrap.sh
#   DEFAULT_INSTALL_DIR=/opt ./scripts/bootstrap.sh
# ==============================================================================

set -euo pipefail

: "${DEFAULT_INSTALL_DIR:=${HOME}}"
: "${VIGIL_DIR:=${DEFAULT_INSTALL_DIR}/vigil}"
: "${VIGIL_REPO_URL:=https://github.com/Vigil-SOC/vigil.git}"
: "${VIGIL_BRANCH:=main}"

if [ "$(id -u)" -eq 0 ]; then
  SUDO=""
else
  SUDO="sudo"
fi

# Step 1: System packages & utilities
echo "--> [1/8] Installing base utilities and TLS prerequisites..."
${SUDO} apt-get update
${SUDO} apt-get install -y \
  ca-certificates \
  curl \
  gnupg \
  lsb-release \
  openssl \
  rsync \
  git \
  mkcert \
  libnss3-tools \
  nmap \
  jq

# Step 2: Detect OS (Debian / Ubuntu)
echo "--> [2/8] Detecting operating system..."
if [ ! -f /etc/os-release ]; then
  echo "Error: Cannot detect operating system (/etc/os-release not found)." >&2
  exit 1
fi

# shellcheck disable=SC1091
. /etc/os-release

DOCKER_OS=""
case "${ID:-}" in
  debian|ubuntu)
    DOCKER_OS="${ID}"
    ;;
  *)
    if [[ "${ID_LIKE:-}" == *ubuntu* ]]; then
      DOCKER_OS="ubuntu"
    elif [[ "${ID_LIKE:-}" == *debian* ]]; then
      DOCKER_OS="debian"
    else
      echo "Error: Unsupported distribution '${ID:-unknown}'. Expected Debian (e.g. Debian 13) or Ubuntu." >&2
      exit 1
    fi
    ;;
esac

CODENAME="${VERSION_CODENAME:-${UBUNTU_CODENAME:-}}"
if [ -z "${CODENAME}" ] && command -v lsb_release >/dev/null 2>&1; then
  CODENAME="$(lsb_release -cs)"
fi
if [ -z "${CODENAME}" ] && [ "${DOCKER_OS}" = "debian" ]; then
  # Fallback for Debian 13 (Trixie) testing/sid environments without VERSION_CODENAME
  CODENAME="trixie"
fi

echo "    Detected: ${PRETTY_NAME:-${DOCKER_OS}} (Docker repo: ${DOCKER_OS}/${CODENAME})"

# Step 3: Configure official Docker APT repository
echo "--> [3/8] Configuring official Docker CE APT repository..."
for pkg in docker.io docker-doc docker-compose docker-compose-v2 podman-docker containerd runc; do
  ${SUDO} apt-get remove -y "${pkg}" >/dev/null 2>&1 || true
done

${SUDO} install -m 0755 -d /etc/apt/keyrings
${SUDO} curl -fsSL "https://download.docker.com/linux/${DOCKER_OS}/gpg" -o /etc/apt/keyrings/docker.asc
${SUDO} chmod a+r /etc/apt/keyrings/docker.asc

echo \
  "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/${DOCKER_OS} ${CODENAME} stable" | \
  ${SUDO} tee /etc/apt/sources.list.d/docker.list >/dev/null

# Step 4: Install Docker CE and Docker Compose plugin
echo "--> [4/8] Installing Docker CE and Docker Compose plugin..."
${SUDO} apt-get update
${SUDO} apt-get install -y \
  docker-ce \
  docker-ce-cli \
  containerd.io \
  docker-buildx-plugin \
  docker-compose-plugin

# Step 5: Enable Docker service, configure user permissions, and ensure Node.js 20+
echo "--> [5/8] Enabling Docker service and checking host prerequisites for start.sh..."
${SUDO} systemctl enable --now docker

TARGET_USER="${SUDO_USER:-${USER:-$(id -un)}}"
NEED_NEWGRP=false
if [ "${TARGET_USER}" != "root" ]; then
  if ! id -nG "${TARGET_USER}" | grep -qw docker; then
    ${SUDO} usermod -aG docker "${TARGET_USER}"
    NEED_NEWGRP=true
    echo "    Added user '${TARGET_USER}' to the 'docker' group."
  fi
fi

# start.sh requires Node.js 20+ and npm on the host for the frontend and TypeScript agent layer.
if ! command -v node >/dev/null 2>&1 || ! node -e "process.exit(parseInt(process.version.slice(1))>=20?0:1)" 2>/dev/null || ! command -v npm >/dev/null 2>&1; then
  echo "    Installing Node.js 20+ (required by ./start.sh for frontend and agent layer)..."
  CANDIDATE_MAJOR="$(apt-cache policy nodejs 2>/dev/null | awk '/Candidate:/ { split($2, a, "."); gsub(/[^0-9]/, "", a[1]); print a[1] }')"
  if [ -n "${CANDIDATE_MAJOR}" ] && [ "${CANDIDATE_MAJOR}" -ge 20 ] 2>/dev/null; then
    # Debian 13 (Trixie) ships Node.js 20+ and npm directly in main
    ${SUDO} apt-get install -y nodejs npm
  else
    # Older Ubuntu/Debian releases: install Node.js 20 LTS from NodeSource
    curl -fsSL https://deb.nodesource.com/gpgkey/nodesource-repo.gpg.key | \
      ${SUDO} gpg --dearmor --yes -o /etc/apt/keyrings/nodesource.gpg
    echo "deb [signed-by=/etc/apt/keyrings/nodesource.gpg] https://deb.nodesource.com/node_20.x nodistro main" | \
      ${SUDO} tee /etc/apt/sources.list.d/nodesource.list >/dev/null
    ${SUDO} apt-get update
    ${SUDO} apt-get install -y nodejs
  fi
fi

# Step 6: Clone or update the Vigil repository
echo "--> [6/8] Downloading Vigil SOC repository into ${VIGIL_DIR}..."
if [ -d "${VIGIL_DIR}/.git" ]; then
  echo "    Existing repository found at ${VIGIL_DIR}; pulling latest changes..."
  git -C "${VIGIL_DIR}" fetch --all --tags
  git -C "${VIGIL_DIR}" pull --ff-only || true
else
  mkdir -p "$(dirname "${VIGIL_DIR}")"
  git clone --branch "${VIGIL_BRANCH}" "${VIGIL_REPO_URL}" "${VIGIL_DIR}"
fi

# Step 7: Initialize environment using Vigil's native scripts/lib.sh helpers
# (avoids conflicting with start.sh's ~/.vigil/jwt_secret and .env expectations)
echo "--> [7/8] Initializing Vigil environment via scripts/lib.sh..."
(
  cd "${VIGIL_DIR}"
  # shellcheck disable=SC1091
  source "${VIGIL_DIR}/scripts/lib.sh"
  load_env
  ensure_jwt_secret
  ensure_agent_token
)

# Step 8: Verify installation & print instructions
echo "--> [8/8] Verifying installation..."
docker --version
docker compose version
node --version

echo ""
echo "=============================================================================="
echo " Vigil SOC Bootstrap Complete!"
echo "=============================================================================="
echo ""
echo " Repository location: ${VIGIL_DIR}"
echo ""
if [ "${NEED_NEWGRP}" = true ]; then
  echo " NOTE: Your user (${TARGET_USER}) was just added to the 'docker' group."
  echo " Run the following command (or log out and back in) before starting Vigil"
  echo " so Docker permissions take effect in your current shell:"
  echo ""
  echo "   newgrp docker"
  echo ""
fi
echo " 1. Navigate to the Vigil directory:"
echo ""
echo "      cd ${VIGIL_DIR}"
echo ""
echo " 2. Start the core Docker containers (PostgreSQL, Redis, Bifrost, etc.):"
echo ""
echo "      docker compose -f infra/docker/docker-compose.yml up -d"
echo ""
echo " 3. Then run ./start.sh to initialize the schema and launch Vigil SOC:"
echo ""
echo "      ./start.sh          # Interactive foreground mode (Ctrl+C to stop)"
echo "      ./start.sh -d       # Background daemon mode (includes 24/7 SOC daemon)"
echo ""
echo " 4. Once started, open the Vigil console in your browser to create your"
echo "    initial administrator account:"
echo ""
echo "      http://localhost:6988"
echo ""
echo "------------------------------------------------------------------------------"
echo " Managing Vigil SOC in the future:"
echo "------------------------------------------------------------------------------"
echo " Whenever you open a new terminal session, return to this directory first:"
echo ""
echo "   cd ${VIGIL_DIR}"
echo ""
echo " From ${VIGIL_DIR}, manage containers and services:"
echo "   docker compose -f infra/docker/docker-compose.yml ps      # Check container status"
echo "   docker compose -f infra/docker/docker-compose.yml logs -f # Follow container logs"
echo "   docker compose -f infra/docker/docker-compose.yml up -d   # Start Docker services"
echo "   ./start.sh                                                # Start Vigil SOC"
echo "   ./shutdown_all.sh                                         # Stop native processes"
echo "   ./shutdown_all.sh -d                                      # Stop everything"
echo "=============================================================================="
