#!/usr/bin/env bash
# ==============================================================================
# Vigil SOC Bootstrap Script (Debian 13 / Ubuntu)
# ==============================================================================
# Installs Docker CE from Docker's official APT repository, clones the Vigil SOC
# repository, prepares the .env configuration (JWT_SECRET_KEY,
# AGENT_INTERNAL_TOKEN, and COMPOSE_FILE), and prints instructions for starting
# and managing Vigil SOC with Docker Compose.
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

# Step 5: Enable Docker service and configure user permissions
echo "--> [5/8] Enabling Docker service and configuring user permissions..."
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

# Step 7: Prepare .env configuration for Docker Compose
echo "--> [7/8] Preparing Vigil environment and secrets..."
ENV_FILE="${VIGIL_DIR}/.env"
if [ ! -f "${ENV_FILE}" ]; then
  (umask 077; cp "${VIGIL_DIR}/env.example" "${ENV_FILE}")
  echo "    Created ${ENV_FILE} from env.example."
fi

# Ensure ~/.vigil/jwt_secret and populate JWT_SECRET_KEY in .env (required by backend container)
VIGIL_STATE_DIR="${HOME}/.vigil"
JWT_SECRET_FILE="${VIGIL_STATE_DIR}/jwt_secret"
if [ ! -s "${JWT_SECRET_FILE}" ]; then
  mkdir -p "${VIGIL_STATE_DIR}"
  chmod 700 "${VIGIL_STATE_DIR}"
  (umask 177; openssl rand -base64 48 | tr -d '\n' > "${JWT_SECRET_FILE}")
fi
JWT_SECRET_VAL="$(cat "${JWT_SECRET_FILE}")"

if grep -qE '^JWT_SECRET_KEY=""' "${ENV_FILE}"; then
  JWT_ESCAPED="$(printf '%s' "${JWT_SECRET_VAL}" | sed -e 's/[\/&|]/\\&/g')"
  sed -i "s|^JWT_SECRET_KEY=\"\"|JWT_SECRET_KEY=\"${JWT_ESCAPED}\"|" "${ENV_FILE}"
  echo "    Generated and set JWT_SECRET_KEY in ${ENV_FILE}."
fi

# Populate AGENT_INTERNAL_TOKEN in .env (required for agent-worker / agent-serve communication)
if grep -qE '^AGENT_INTERNAL_TOKEN=""' "${ENV_FILE}"; then
  AGENT_TOKEN_VAL="$(openssl rand -hex 32)"
  sed -i "s|^AGENT_INTERNAL_TOKEN=\"\"|AGENT_INTERNAL_TOKEN=\"${AGENT_TOKEN_VAL}\"|" "${ENV_FILE}"
  echo "    Generated and set AGENT_INTERNAL_TOKEN in ${ENV_FILE}."
fi

# Point COMPOSE_FILE to infra/docker/docker-compose.yml and link infra/docker/.env -> ../../.env
# so plain `docker compose` commands work directly from ${VIGIL_DIR}.
if ! grep -qE '^COMPOSE_FILE=' "${ENV_FILE}"; then
  printf '\n# Docker Compose file location (enables running `docker compose` from repo root)\nCOMPOSE_FILE="%s/infra/docker/docker-compose.yml"\n' "${VIGIL_DIR}" >> "${ENV_FILE}"
fi
ln -sf ../../.env "${VIGIL_DIR}/infra/docker/.env"

# Step 8: Verify installation & print instructions
echo "--> [8/8] Verifying Docker installation..."
docker --version
docker compose version

echo ""
echo "=============================================================================="
echo " Vigil SOC Bootstrap Complete!"
echo "=============================================================================="
echo ""
echo " Repository location: ${VIGIL_DIR}"
echo " Environment config:  ${ENV_FILE}"
echo ""
if [ "${NEED_NEWGRP}" = true ]; then
  echo " NOTE: Your user (${TARGET_USER}) was just added to the 'docker' group."
  echo " Run the following command (or log out and back in) so group permissions"
  echo " take effect in your current shell:"
  echo ""
  echo "   newgrp docker"
  echo ""
fi
echo " 1. Navigate to the Vigil directory:"
echo ""
echo "      cd ${VIGIL_DIR}"
echo ""
echo " 2. Start Vigil SOC with Docker Compose for the first time:"
echo ""
echo "      docker compose up -d"
echo ""
echo "    (Optional: include the 24/7 autonomous SOC daemon and LLM worker:)"
echo "      docker compose --profile daemon up -d"
echo ""
echo " 3. Once the containers are running, open the Vigil console in your browser"
echo "    to create your initial administrator account:"
echo ""
echo "      http://localhost:6987"
echo ""
echo "------------------------------------------------------------------------------"
echo " Managing Vigil SOC in the future:"
echo "------------------------------------------------------------------------------"
echo " Whenever you open a new terminal session, return to this directory first:"
echo ""
echo "   cd ${VIGIL_DIR}"
echo ""
echo " From ${VIGIL_DIR}, you can run any Docker Compose command:"
echo "   docker compose ps                  # Check container status"
echo "   docker compose logs -f             # Follow live container logs"
echo "   docker compose stop                # Stop containers (preserving state)"
echo "   docker compose up -d               # Start containers in the background"
echo "   docker compose down                # Stop and remove containers"
echo "=============================================================================="
