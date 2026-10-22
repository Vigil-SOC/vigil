FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive

# python-is-python3: CI's setup-python puts `python` on PATH and the workflows
# call it bare (`python scripts/check_one_loop.py`, `python -c ...init_database()`).
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    curl \
    git \
    gnupg \
    python3 \
    python3-pip \
    python3-venv \
    python-is-python3 \
    sudo \
    xz-utils \
    && rm -rf /var/lib/apt/lists/*

########################################################
# NODE
########################################################

# The web, OpenAPI, and npm-audit jobs in ci-cd.yml and nightly.yml pin Node 20
# (npm 10). Ubuntu 24.04's apt npm is 9.2.0, whose `npm audit --audit-level=high`
# exits 0 even with a high advisory present, so the Security Scan - NPM gate
# could not be checked here. Official tarball, checksum from its SHASUMS256.txt.
# /usr/local/bin precedes /usr/bin, but a cloud agent's shell is given the
# harness Node (22 today) ahead of both, so `node` and `#!/usr/bin/env node`
# were not this binary. The selector further down puts it back in front.
RUN ARCH="$(dpkg --print-architecture)" \
    && case "$ARCH" in \
         amd64) NARCH=x64; NSUM=df770b2a6f130ed8627c9782c988fda9669fa23898329a61a871e32f965e007d ;; \
         arm64) NARCH=arm64; NSUM=73093db209e4e9e09dd7d15a47aeaab1b74833830df03efa5f942a1122c5fa71 ;; \
         *) echo "unsupported arch $ARCH" && exit 1 ;; \
       esac \
    && NODE_VERSION=v20.20.2 \
    && curl --retry 3 --retry-delay 5 -fsSL -o /tmp/node.tar.xz \
       "https://nodejs.org/dist/${NODE_VERSION}/node-${NODE_VERSION}-linux-${NARCH}.tar.xz" \
    && echo "${NSUM}  /tmp/node.tar.xz" | sha256sum -c - \
    && tar -xJf /tmp/node.tar.xz -C /usr/local --strip-components=1 \
       --exclude='*/CHANGELOG.md' --exclude='*/LICENSE' --exclude='*/README.md' \
    && rm -f /tmp/node.tar.xz \
    && node --version && npm --version

# test-agent-layer pins Node 22 because services/agent/eval/estate.ts imports
# node:sqlite, which Node 20 does not ship. This is the release setup-node
# resolves for node-version: '22' (npm 10.9.9). It stays off the default PATH;
# the selector uses it for services/agent only.
RUN ARCH="$(dpkg --print-architecture)" \
    && case "$ARCH" in \
         amd64) NARCH=x64; NSUM=1805bcd35e6815559bd2eb97db438d3fdb0eb3a5e6a977b7c1badca76337e9ce ;; \
         arm64) NARCH=arm64; NSUM=6287aa46b6c375d816e63ae66d024fbbfd70404825aabdfdf898a970871d2f4b ;; \
         *) echo "unsupported arch $ARCH" && exit 1 ;; \
       esac \
    && NODE_VERSION=v22.23.4 \
    && curl --retry 3 --retry-delay 5 -fsSL -o /tmp/node22.tar.xz \
       "https://nodejs.org/dist/${NODE_VERSION}/node-${NODE_VERSION}-linux-${NARCH}.tar.xz" \
    && echo "${NSUM}  /tmp/node22.tar.xz" | sha256sum -c - \
    && mkdir -p /usr/local/lib/node-v22 \
    && tar -xJf /tmp/node22.tar.xz -C /usr/local/lib/node-v22 --strip-components=1 \
    && rm -f /tmp/node22.tar.xz \
    && /usr/local/lib/node-v22/bin/node --version \
    && /usr/local/lib/node-v22/bin/node -e "import('node:sqlite').then(()=>{})"

# `node` / `npm` / `npx` on PATH, and the `node` that `#!/usr/bin/env node`
# finds, follow CI's split: Node 20 everywhere except services/agent, which is
# Node 22. Bash functions cover a restored PATH that puts the harness node
# first again; the directory prepend covers shebangs in start/install, which
# do not go through that restore. BASH_ENV is how a non-interactive agent
# shell (bash -c, not a login shell) picks this up.
RUN python3 - <<'PY'
from pathlib import Path
import os

root = Path("/usr/local/lib/ci-node")
bindir = root / "bin"
bindir.mkdir(parents=True, exist_ok=True)

(root / "select.sh").write_text(
    """ci_node_dir() {
  case "${PWD:-}" in
    */services/agent|*/services/agent/*)
      printf '%s\\n' /usr/local/lib/node-v22/bin
      return 0
      ;;
  esac
  for arg in "$@"; do
    case "$arg" in
      *services/agent*)
        printf '%s\\n' /usr/local/lib/node-v22/bin
        return 0
        ;;
    esac
  done
  printf '%s\\n' /usr/local/lib/ci-node20/bin
}
"""
)

dispatch = bindir / "dispatch"
dispatch.write_text(
    """#!/bin/bash
set -euo pipefail
. /usr/local/lib/ci-node/select.sh
tool="$(basename "$0")"
dir="$(ci_node_dir "$@")"
export PATH="${dir}:${PATH:-}"
exec "${dir}/${tool}" "$@"
"""
)
os.chmod(dispatch, 0o755)
for name in ("node", "npm", "npx", "corepack"):
    link = bindir / name
    if link.is_symlink() or link.exists():
        link.unlink()
    link.symlink_to("dispatch")

n20 = Path("/usr/local/lib/ci-node20/bin")
n20.mkdir(parents=True, exist_ok=True)
for name in ("node", "npm", "npx", "corepack"):
    dest = n20 / name
    if dest.is_symlink() or dest.exists():
        dest.unlink()
    dest.symlink_to(f"/usr/local/bin/{name}")

(root / "bash-env.sh").write_text(
    """ci=/usr/local/lib/ci-node/bin
case ":${PATH:-}:" in
  *:"$ci":*) ;;
  *) PATH="${ci}${PATH:+:$PATH}"; export PATH ;;
esac
node() { command /usr/local/lib/ci-node/bin/node "$@"; }
npm() { command /usr/local/lib/ci-node/bin/npm "$@"; }
npx() { command /usr/local/lib/ci-node/bin/npx "$@"; }
corepack() { command /usr/local/lib/ci-node/bin/corepack "$@"; }
"""
)
PY
RUN mkdir -p /tmp/ci-node-smoke/services/agent \
    && BASH_ENV=/usr/local/lib/ci-node/bash-env.sh bash -c 'node --version' | grep -qx 'v20.20.2' \
    && BASH_ENV=/usr/local/lib/ci-node/bash-env.sh bash -c 'cd /tmp/ci-node-smoke/services/agent && node --version' | grep -qx 'v22.23.4' \
    && BASH_ENV=/usr/local/lib/ci-node/bash-env.sh bash -c 'cd /tmp/ci-node-smoke/services/agent && node -e "import(\"node:sqlite\").then(()=>{})"' \
    && rm -rf /tmp/ci-node-smoke
ENV BASH_ENV=/usr/local/lib/ci-node/bash-env.sh

########################################################
# DOCKER INSTALLATION
########################################################

# Install Docker
RUN install -m 0755 -d /etc/apt/keyrings && \
    curl --retry 3 --retry-delay 5 -fsSL https://download.docker.com/linux/ubuntu/gpg | gpg --dearmor -o /etc/apt/keyrings/docker.gpg && \
    chmod a+r /etc/apt/keyrings/docker.gpg && \
    echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu \
    $(. /etc/os-release && echo "$VERSION_CODENAME") stable" | tee /etc/apt/sources.list.d/docker.list > /dev/null && \
    apt-get update && \
    apt-get install -y \
    docker-ce=5:28.5.2-1~ubuntu.24.04~noble \
    docker-ce-cli=5:28.5.2-1~ubuntu.24.04~noble \
    containerd.io \
    docker-buildx-plugin \
    docker-compose-plugin \
    && rm -rf /var/lib/apt/lists/*

RUN apt-get update && apt-get install -y fuse-overlayfs && rm -rf /var/lib/apt/lists/*
# `start` pulls postgres, redis and bifrost from Docker Hub on every boot, and
# unauthenticated pulls from shared cloud egress hit Hub's rate limit
# (`toomanyrequests`), failing the whole boot. mirror.gcr.io serves the same
# digests; the daemon falls back to Hub for anything the mirror lacks.
RUN mkdir -p /etc/docker && \
    printf '%s\n' '{' \
    '  "storage-driver": "fuse-overlayfs",' \
    '  "registry-mirrors": ["https://mirror.gcr.io"]' \
    '}' > /etc/docker/daemon.json
RUN apt-get update && apt-get install -y iptables && rm -rf /var/lib/apt/lists/*
RUN update-alternatives --set iptables /usr/sbin/iptables-legacy && \
    update-alternatives --set ip6tables /usr/sbin/ip6tables-legacy

########################################################
# HELM
########################################################

# Pin matches .github/workflows/helm-chart.yml so `helm template` matches CI.
RUN ARCH="$(dpkg --print-architecture)" \
    && HELM_VERSION=v3.15.2 \
    && cd /tmp \
    && curl --retry 3 --retry-delay 5 -fsSL -O "https://get.helm.sh/helm-${HELM_VERSION}-linux-${ARCH}.tar.gz" \
    && curl --retry 3 --retry-delay 5 -fsSL -O "https://get.helm.sh/helm-${HELM_VERSION}-linux-${ARCH}.tar.gz.sha256sum" \
    && sha256sum -c "helm-${HELM_VERSION}-linux-${ARCH}.tar.gz.sha256sum" \
    && tar -xzf "helm-${HELM_VERSION}-linux-${ARCH}.tar.gz" \
    && mv "linux-${ARCH}/helm" /usr/local/bin/helm \
    && rm -rf "/tmp/helm-${HELM_VERSION}-linux-${ARCH}.tar.gz" \
              "/tmp/helm-${HELM_VERSION}-linux-${ARCH}.tar.gz.sha256sum" \
              "/tmp/linux-${ARCH}" \
    && helm version --short

# helm-chart.yml downloads kubeconform v0.6.7 and runs
# `kubeconform -strict -ignore-missing-schemas` over the default, dev and
# Bitnami renders. Same pin, with the checksums from the release's CHECKSUMS.
RUN ARCH="$(dpkg --print-architecture)" \
    && case "$ARCH" in \
         amd64) KSUM=95f14e87aa28c09d5941f11bd024c1d02fdc0303ccaa23f61cef67bc92619d73 ;; \
         arm64) KSUM=dc82f79bb03c5479b1ae5fd4af221e4b5a3111f62bf01a2795d9c5c20fa96644 ;; \
         *) echo "unsupported arch $ARCH" && exit 1 ;; \
       esac \
    && KUBECONFORM_VERSION=v0.6.7 \
    && cd /tmp \
    && curl --retry 3 --retry-delay 5 -fsSL -o kubeconform.tar.gz \
       "https://github.com/yannh/kubeconform/releases/download/${KUBECONFORM_VERSION}/kubeconform-linux-${ARCH}.tar.gz" \
    && echo "${KSUM}  kubeconform.tar.gz" | sha256sum -c - \
    && tar -xzf kubeconform.tar.gz -C /usr/local/bin kubeconform \
    && rm -f kubeconform.tar.gz \
    && kubeconform -v

# ci-cd.yml's Helm Install Schema job and medic-helm.yml both run
# `yq -i 'del(.dependencies)'` before `helm template`, and the schema job
# then reads the rendered Job with `yq -0`. Neither workflow installs yq:
# ubuntu-latest ships mikefarah yq 4.54.1 (image 20261012.334.1). This base
# does not, so that command cannot be run here. Same version. The SHA256 is
# field 19 of the release `checksums` file, which is the field the runner
# image checks.
RUN ARCH="$(dpkg --print-architecture)" \
    && case "$ARCH" in \
         amd64) YSUM=8e34fc298390875de416e6a4afcb8cabeceb25d9aa8506c1a2f9353cf702ea5f ;; \
         arm64) YSUM=189088da0c6429ec5178dfaab1a114805f6cab0b61b165ab236efedf1d57a71b ;; \
         *) echo "unsupported arch $ARCH" && exit 1 ;; \
       esac \
    && YQ_VERSION=v4.54.1 \
    && curl --retry 3 --retry-delay 5 -fsSL -o /tmp/yq \
       "https://github.com/mikefarah/yq/releases/download/${YQ_VERSION}/yq_linux_${ARCH}" \
    && echo "${YSUM}  /tmp/yq" | sha256sum -c - \
    && install -m 0755 /tmp/yq /usr/local/bin/yq \
    && rm -f /tmp/yq \
    && yq --version

########################################################
# ACTIONLINT + HADOLINT (CI lint gates)
########################################################

# ci-cd.yml gates workflow edits with `docker://rhysd/actionlint:1.7.12` and
# Dockerfiles with `hadolint/hadolint-action@v3.5.0` (hadolint v2.15.1). Both
# are static binaries pinned to the same versions so a local run reports what
# CI will. actionlint reads .github/actionlint.yaml from the repo root, which
# keeps shellcheck findings out of the gate, so shellcheck is not needed here.
RUN ARCH="$(dpkg --print-architecture)" \
    && ACTIONLINT_VERSION=1.7.12 \
    && curl --retry 3 --retry-delay 5 -fsSL \
       "https://github.com/rhysd/actionlint/releases/download/v${ACTIONLINT_VERSION}/actionlint_${ACTIONLINT_VERSION}_linux_${ARCH}.tar.gz" \
       | tar -xz -C /usr/local/bin actionlint \
    && actionlint -version \
    && case "$ARCH" in amd64) HL_ARCH=x86_64 ;; arm64) HL_ARCH=arm64 ;; *) echo "unsupported arch $ARCH" && exit 1 ;; esac \
    && HADOLINT_VERSION=v2.15.1 \
    && curl --retry 3 --retry-delay 5 -fsSL -o /usr/local/bin/hadolint \
       "https://github.com/hadolint/hadolint/releases/download/${HADOLINT_VERSION}/hadolint-Linux-${HL_ARCH}" \
    && chmod +x /usr/local/bin/hadolint \
    && hadolint --version

########################################################
# AGENT-BROWSER (live tests)
########################################################

# The factory asks every implementer to exercise its change live, with a
# browser where there is a UI. agent-browser is a native binary; the release
# asset is used rather than the npm package, whose wrapper wants Node 24 while
# this repo's CI pins Node 20. Pinned like Helm above.
# `install --with-deps` is run as root for Chrome's shared libraries; the
# browser download itself lands per-user under ~/.agent-browser, so the ubuntu
# user fetches its own copy below and root's is discarded.
RUN ARCH="$(dpkg --print-architecture)" \
    && case "$ARCH" in amd64) AB_ARCH=x64 ;; arm64) AB_ARCH=arm64 ;; *) echo "unsupported arch $ARCH" && exit 1 ;; esac \
    && AB_VERSION=v0.36.0 \
    && curl --retry 3 --retry-delay 5 -fsSL -o /usr/local/bin/agent-browser \
       "https://github.com/vercel-labs/agent-browser/releases/download/${AB_VERSION}/agent-browser-linux-${AB_ARCH}" \
    && chmod +x /usr/local/bin/agent-browser \
    && agent-browser --version \
    && apt-get update \
    && agent-browser install --with-deps \
    && rm -rf /root/.agent-browser /var/lib/apt/lists/*

# The CLI serves its own usage guide via `agent-browser skills get core`, which
# its discovery stub tells agents to read first. The binary finds that content
# in the npm package's `skill-data/` next to it; the release asset ships none,
# so `skills` fails with "Skills directory not found". Fetch `skill-data/` from
# the same tag and point the CLI at it.
RUN AB_VERSION=v0.36.0 \
    && mkdir -p /usr/local/share/agent-browser \
    && curl --retry 3 --retry-delay 5 -fsSL \
       "https://github.com/vercel-labs/agent-browser/archive/refs/tags/${AB_VERSION}.tar.gz" \
       | tar -xz -C /usr/local/share/agent-browser --strip-components=1 \
             "agent-browser-${AB_VERSION#v}/skill-data" \
    && AGENT_BROWSER_SKILLS_DIR=/usr/local/share/agent-browser/skill-data agent-browser skills get core >/dev/null
ENV AGENT_BROWSER_SKILLS_DIR=/usr/local/share/agent-browser/skill-data

# Chrome's user-namespace sandbox is unavailable under Ubuntu 24.04's AppArmor
# defaults and inside VMs; without this every launch dies before DevTools starts.
ENV AGENT_BROWSER_ARGS="--no-sandbox"

########################################################
# RESTIC (integration backup tests)
########################################################

# ci-cd.yml "Install restic and pg_dump" pins restic 0.19.1 and unpacks the
# release with bunzip2 before tests/integration. Backup create and restore
# shell out to `restic`. pg_dump 16 is already the Ubuntu 24.04 client.
# Checksums are the ones in that workflow.
RUN apt-get update && apt-get install -y --no-install-recommends bzip2 \
    && rm -rf /var/lib/apt/lists/* \
    && ARCH="$(dpkg --print-architecture)" \
    && case "$ARCH" in \
         amd64) RARCH=amd64; SHA=f415415624dcc452f2a02b8c33641791a8c6d6d3b65bbb3543fcf9a25151585c ;; \
         arm64) RARCH=arm64; SHA=a5f64aaab53d51e311fa3829124c5b703f2d14cf187d8640b6be3b2b49376465 ;; \
         *) echo "unsupported arch $ARCH" && exit 1 ;; \
       esac \
    && RESTIC_VERSION=0.19.1 \
    && curl --retry 3 --retry-delay 5 -fsSL -o /tmp/restic.bz2 \
       "https://github.com/restic/restic/releases/download/v${RESTIC_VERSION}/restic_${RESTIC_VERSION}_linux_${RARCH}.bz2" \
    && printf '%s  %s\n' "$SHA" /tmp/restic.bz2 > /tmp/restic.bz2.sha256 \
    && sha256sum -c /tmp/restic.bz2.sha256 \
    && bunzip2 -f /tmp/restic.bz2 \
    && install -m 0755 /tmp/restic /usr/local/bin/restic \
    && rm -f /tmp/restic /tmp/restic.bz2.sha256 \
    && restic version

########################################################
# CONFIG UBUNTU USER
########################################################

# ensure no password authentication
RUN mkdir -p /etc/ssh/sshd_config.d && \
    echo 'PasswordAuthentication no\nChallengeResponseAuthentication no\nUsePAM no' > /etc/ssh/sshd_config.d/disable_password_auth.conf

# Create non-root user (only if it doesn't exist)
RUN id -u ubuntu &>/dev/null || useradd -m -s /bin/bash ubuntu
# Create docker group if it doesn't exist and add ubuntu user to it
RUN groupadd -f docker && usermod -aG docker ubuntu
RUN usermod -aG sudo ubuntu
# Configure passwordless sudo for ubuntu user
RUN echo "ubuntu ALL=(ALL) NOPASSWD:ALL" > /etc/sudoers.d/ubuntu
# Set a password for ubuntu user
RUN echo "ubuntu:ubuntu" | chpasswd

USER ubuntu

# CI's DB-backed unit job (`pytest tests/unit -m external_service`) gets its
# POSTGRES_* from job env. tests/conftest.py turns .env loading off, as CI
# does, so a bare shell hits MissingPostgresPasswordError and the throwaway-
# database fixture errors. The password alone is enough: host, port and user
# default to the compose Postgres that `start` brings up, and the fixture only
# uses it to CREATE/DROP its own vigil_test_* database. It is the published
# compose default, the same value `start` and .env already use.
ENV POSTGRES_PASSWORD=deeptempo_secure_password_change_me

# Every pytest job in ci-cd.yml and nightly.yml sets TESTING=true. Without it a
# test that enters TestClient as a context manager runs the real app lifespan,
# which dials Bifrost, the model catalog and the MCP servers, and never
# finishes (tests/unit/auth/test_token_type.py hangs even with the stack up).
# `start` writes TESTING=false into .env, which every stack launcher sources,
# so the live app still connects to its integrations.
ENV TESTING=true

# Chrome for Testing, into /home/ubuntu/.agent-browser (system libraries are already in place).
RUN agent-browser install
