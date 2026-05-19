# GitHub Actions Workflows

This directory contains the CI/CD workflows for AI-OpenSOC.

## Current Configuration: CI Only (Testing + Building)

The workflows are currently configured for **Continuous Integration** only - they test your code and build Docker images, but **don't deploy anywhere**.

### What Runs Automatically

✅ **On every push/PR**:
- Linting (Python, TypeScript, Dockerfile)
- Unit tests with coverage
- Integration tests  
- Security scanning
- Docker image building
- Container vulnerability scanning

✅ **Result**: 
- Tests verify your code works
- Docker images pushed to GitHub Container Registry
- No deployment needed!

## Workflows

### 1. `ci-cd.yml` - Main Testing Pipeline
- **Triggers**: Push or PR to main/develop
- **Purpose**: Test everything and build images
- **Deployment**: Disabled (testing only)

### 2. `release-please.yml` - Automated Release PRs
- **Triggers**: Push to `main`, manual dispatch
- **Purpose**: Read Conventional Commits since the last tag; open or update a release PR that bumps `VERSION`, `helm/vigil/Chart.yaml` (both `appVersion` and `version`, in lockstep), `frontend/package.json`, and `frontend/package-lock.json`, and updates `CHANGELOG.md`. On merge, push the `vX.Y.Z` tag and create the GitHub Release. See `RELEASING.md`.
- **Deployment**: None (tagging only — downstream `release.yml` handles deploys)

### 3. `release.yml` - Tag-Triggered Production Pipeline
- **Triggers**: Version tags (`v*.*.*`)
- **Purpose**: Build production images, Trivy scan, deploy to production VMs, post-deploy validation. The GitHub Release object is created by `release-please.yml`, not this workflow.
- **Deployment**: Production (SSH to `PROD_VM_HOST` via `scripts/deploy_to_vm.sh production`, runs health checks, posts Slack notifications). Requires `SSH_PRIVATE_KEY`, `PROD_VM_HOST`, `PROD_VM_USER`, and `SLACK_WEBHOOK_URL` secrets.

### 4. `nightly.yml` - Scheduled Testing
- **Triggers**: Daily at 2 AM UTC
- **Purpose**: Comprehensive testing and security audits
- **Deployment**: None

## No Secrets Required!

Since deployment is disabled, you **don't need to configure**:
- ❌ SSH_PRIVATE_KEY
- ❌ VM_HOST variables  
- ❌ SLACK_WEBHOOK_URL
- ❌ SENTRY_DSN

The only "secret" used is `GITHUB_TOKEN` which is **automatically provided** by GitHub Actions.

## Running Your App Manually

After the CI builds your images, you can run them anywhere:

```bash
# Pull the built images
docker pull ghcr.io/vigil-soc/vigil-backend:main
docker pull ghcr.io/vigil-soc/vigil-daemon:main

# Run with docker-compose
docker-compose up -d
```

## Future: Enabling Deployment

When you're ready to auto-deploy, see:
- `docs/CI_CD_GUIDE.md` - Full CI/CD documentation
- `docs/DEPLOYMENT_GUIDE.md` - VM deployment guide

To enable:
1. Uncomment the `deploy-staging` job in `ci-cd.yml`
2. Add required secrets to GitHub
3. Configure your VMs

But for now - **pure CI testing works perfectly!** ✅

