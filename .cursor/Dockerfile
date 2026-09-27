FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive

# python-is-python3: CI's setup-python puts `python` on PATH and the workflows
# call it bare (`python scripts/check_one_loop.py`, `python -c ...init_database()`).
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    curl \
    git \
    gnupg \
    nodejs \
    npm \
    python3 \
    python3-pip \
    python3-venv \
    python-is-python3 \
    sudo \
    && rm -rf /var/lib/apt/lists/*

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
RUN mkdir -p /etc/docker && \
    printf '%s\n' '{' \
    '  "storage-driver": "fuse-overlayfs"' \
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
# this repo's CI and frontend pin Node 18. Pinned like Helm above.
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

# Chrome for Testing, into /home/ubuntu/.agent-browser (system libraries are already in place).
RUN agent-browser install
