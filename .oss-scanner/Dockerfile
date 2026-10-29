# OSS Scanner build for VigilSOC (https://github.com/Vigil-SOC/vigil).
#
# Enrolled in https://github.com/anthropics/oss-scanner as projects/vigilsoc.
# The scanner builds this file with a fresh clone of this repository as the
# context. The network is available only while the image builds; the scan runs
# offline, so everything the backend and the web client need is installed here.
#
# What the image holds, at /src:
#   - Python (version from .python-version) with requirements.lock and the dev
#     toolchain installed system-wide, so `uvicorn` and `flake8` work with no
#     venv.
#   - clients/web/node_modules and a production build, so the API serves the
#     SPA.
#   - services/agent/node_modules and its tsc build (the TypeScript agent).
#   - PostgreSQL (bookworm ships 15; CI uses 16) and Redis, so the API and
#     daemon can run on localhost.
#
# DEV_MODE is off (auth on, as a fresh install) and JWT_SECRET_KEY is a fixed
# throwaway value that exists only so the backend imports; it is not a secret
# and no deployment uses it.
#
# Start the API (auth on, a fresh install):
#   /usr/local/bin/vigil-services-up
#   uvicorn services.api.main:app --host 127.0.0.1 --port 6987
ARG PYTHON_VERSION=3.12
FROM python:${PYTHON_VERSION}-slim-bookworm

ENV DEBIAN_FRONTEND=noninteractive \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONDONTWRITEBYTECODE=1

# build-essential/libpq-dev: wheels without a manylinux build. postgresql and
# redis-server: local services for the API. nodejs from
# NodeSource at the major CI uses (NODE_VERSION in ci-cd.yml).
RUN apt-get update && apt-get install -y --no-install-recommends \
      ca-certificates curl gnupg git build-essential libpq-dev \
      postgresql postgresql-contrib redis-server \
    && curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/*

# scanner contract: the checkout lives inside the image, at /src
COPY . /src
WORKDIR /src

# Python deps: the same lock CI installs, plus the lint/type toolchain.
RUN pip install -r requirements.lock -r requirements-dev.txt

# Web client deps (eslint, the build toolchain) fetched now; the scan
# has no registry access.
# The build lets the API serve the SPA at /, as the release image does.
RUN cd clients/web && npm ci --no-audit --no-fund && npm run build

# The TypeScript agent service (services/agent): deps and a compiled build.
RUN cd services/agent && npm ci --no-audit --no-fund && npm run build

# Local Postgres matching the CI service (ci-cd.yml, test-helm-install-schema) and
# a helper that starts it and Redis. pg_trgm is created up front: the schema
# needs it and the scan cannot fetch anything.
ENV POSTGRES_HOST=localhost \
    POSTGRES_PORT=5432 \
    POSTGRES_USER=test \
    POSTGRES_PASSWORD=test \
    POSTGRES_DB=deeptempo_test \
    REDIS_URL=redis://localhost:6379/0 \
    TESTING=true \
    DEV_MODE=false \
    JWT_SECRET_KEY=oss-scanner-only-not-a-real-secret
RUN printf '%s\n' \
      '#!/bin/sh' \
      'set -e' \
      'service postgresql start >/dev/null' \
      'until pg_isready -q -h localhost; do sleep 0.5; done' \
      'redis-server --daemonize yes >/dev/null' \
      'echo "postgres and redis up on localhost"' \
      > /usr/local/bin/vigil-services-up \
    && chmod +x /usr/local/bin/vigil-services-up \
    && service postgresql start \
    && su postgres -c "psql -v ON_ERROR_STOP=1 -c \"CREATE ROLE test LOGIN SUPERUSER PASSWORD 'test';\"" \
    && su postgres -c "createdb -O test deeptempo_test" \
    && su postgres -c "psql -d deeptempo_test -c 'CREATE EXTENSION IF NOT EXISTS pg_trgm;'" \
    && service postgresql stop

# Fail the build, not the scan, if the backend cannot import.
RUN python -c "import services.api.main"
