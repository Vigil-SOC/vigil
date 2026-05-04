# CI/CD Guide

Complete guide to the CI/CD pipeline for Vigil.

## Table of Contents

- [Overview](#overview)
- [GitHub Actions Workflows](#github-actions-workflows)
- [Pipeline Stages](#pipeline-stages)
- [Secrets Configuration](#secrets-configuration)
- [Deployment Process](#deployment-process)
- [Monitoring & Alerts](#monitoring--alerts)

---

## Overview

Vigil uses **GitHub Actions** for CI/CD with three main workflows:

1. **ci-cd.yml**: Main pipeline for PRs and pushes
2. **release.yml**: Production deployment on tags
3. **nightly.yml**: Scheduled comprehensive testing

### Pipeline Architecture

```
┌──────────────┐
│ Pull Request │
│  or Push     │
└──────┬───────┘
       │
       ▼
┌──────────────┐
│   Linting    │
│ Backend/FE   │
└──────┬───────┘
       │
       ▼
┌──────────────┐
│  Unit Tests  │
│ Backend/FE   │
└──────┬───────┘
       │
       ▼
┌──────────────┐
│ Integration  │
│    Tests     │
└──────┬───────┘
       │
       ▼
┌──────────────┐
│   Security   │
│   Scanning   │
└──────┬───────┘
       │
       ▼
┌──────────────┐
│ Build Docker │
│    Images    │
└──────┬───────┘
       │
       ▼ (main branch only)
┌──────────────┐
│Deploy Staging│
└──────────────┘
```

---

## GitHub Actions Workflows

### Main CI/CD Workflow

**File**: `.github/workflows/ci-cd.yml`

**Triggers**:
- Pull requests to `main` or `develop`
- Pushes to `main` or `develop`

**Jobs**:
1. `lint-backend` - Python linting (flake8, black, isort, mypy)
2. `lint-frontend` - TypeScript/React linting (ESLint)
3. `lint-docker` - Dockerfile linting (hadolint)
4. `test-unit-backend` - Backend unit tests with coverage
5. `test-unit-frontend` - Frontend unit tests
6. `test-integration` - Integration tests with PostgreSQL
7. `security-scan-python` - Bandit SAST scanning
8. `security-scan-npm` - NPM audit
9. `build-images` - Build and push Docker images
10. `build-frontend` - Build frontend static assets
11. `scan-images` - Trivy vulnerability scanning
12. `deploy-staging` - Deploy to staging (main branch only)

### Release Workflow

**File**: `.github/workflows/release.yml`

**Triggers**:
- Tags matching `v*.*.*` (e.g., v1.0.0)

**Jobs**:
1. `create-release` - Create GitHub release with changelog
2. `build-production-images` - Build production Docker images
3. `scan-production-images` - Security scan production images
4. `deploy-production` - Deploy to production with rollback capability
5. `post-deployment-validation` - Smoke tests and health checks

### Nightly Tests

**File**: `.github/workflows/nightly.yml`

**Triggers**:
- Scheduled: 2 AM UTC daily
- Manual: workflow_dispatch

**Jobs**:
1. `full-test-suite` - Complete test coverage
2. `performance-tests` - Performance regression testing
3. `security-audit` - pip-audit vulnerability scan
4. `migration-tests` - Database migration validation

---

## Pipeline Stages

### Stage 1: Linting & Code Quality

**Backend Linting**:
```yaml
- flake8: Style checking
- black: Code formatting
- isort: Import sorting
- mypy: Type checking
```

**Frontend Linting**:
```yaml
- ESLint: JavaScript/TypeScript linting
- Prettier: Code formatting (optional)
```

**Exit Criteria**:
- All linters pass
- No critical issues found

### Stage 2: Unit Tests

**Backend**:
```bash
pytest tests/unit/ -v --cov=backend --cov=services --cov=daemon
```

**Frontend**:
```bash
npm run test:unit
```

**Exit Criteria**:
- All tests pass
- Coverage thresholds met (80% backend, 70% frontend)

### Stage 3: Integration Tests

**Setup**:
- PostgreSQL container via GitHub Actions services
- Test database: `deeptempo_test`

**Execution**:
```bash
pytest tests/integration/ -v --cov
```

**Exit Criteria**:
- All integration tests pass
- No database errors
- External API mocks working

### Stage 4: Security Scanning

**Python (Bandit)**:
```bash
bandit -r backend/ services/ daemon/ -f json -o bandit-report.json
```

**NPM**:
```bash
npm audit --audit-level=moderate
```

**Exit Criteria**:
- No critical vulnerabilities
- High severity issues triaged
- Report uploaded as artifact

### Stage 5: Build Images

**Backend Image**:
```dockerfile
FROM python:3.10-slim
COPY . /app
RUN pip install -r requirements.txt
CMD ["uvicorn", "backend.main:app", "--host", "0.0.0.0"]
```

**Daemon Image**:
```dockerfile
FROM python:3.10-slim
COPY . /app
CMD ["python", "-m", "daemon.main"]
```

**Optimization**:
- Docker BuildKit for caching
- Multi-stage builds
- Layer caching via GitHub Actions

**Exit Criteria**:
- Images build successfully
- Tagged with branch and SHA
- Pushed to GitHub Container Registry

### Stage 6: Container Scanning

**Trivy Scanner**:
```bash
trivy image --severity CRITICAL,HIGH ghcr.io/user/image:tag
```

**Exit Criteria**:
- No critical vulnerabilities
- Scan results uploaded to GitHub Security

### Stage 7: Deployment

**Staging** (automatic on main):
```bash
./scripts/deploy_to_vm.sh staging
```

**Production** (manual on release tag):
```bash
./scripts/deploy_to_vm.sh production
```

**Exit Criteria**:
- Services healthy
- Smoke tests pass
- Rollback available

---

## Secrets Configuration

### Required Secrets

Configure in **Settings → Secrets → Actions**:

#### SSH & VM Access
```
SSH_PRIVATE_KEY         # SSH key for VM access
STAGING_VM_HOST         # Staging VM hostname/IP
STAGING_VM_USER         # SSH username (staging)
PROD_VM_HOST            # Production VM hostname/IP
PROD_VM_USER            # SSH username (production)
```

#### Container Registry
```
GITHUB_TOKEN            # Auto-provided by GitHub Actions
```

#### API Keys
```
ANTHROPIC_API_KEY       # Claude API key for testing
```

#### Notifications
```
SLACK_WEBHOOK_URL       # Slack webhook for deployment notifications
```

#### Monitoring
```
SENTRY_DSN              # Sentry error tracking DSN
```

### Setting Secrets

```bash
# Via GitHub CLI
gh secret set SSH_PRIVATE_KEY < ~/.ssh/id_rsa
gh secret set SLACK_WEBHOOK_URL -b "https://hooks.slack.com/..."

# Via GitHub Web UI
1. Go to repository Settings
2. Click Secrets → Actions
3. Click "New repository secret"
4. Add name and value
```

---

## Deployment Process

### Staging Deployment

**Automatic on main branch merge**:

1. All tests pass
2. Images built and scanned
3. SSH to staging VM
4. Pull latest images
5. Run migrations
6. Rolling restart services
7. Health checks
8. Slack notification

**Manual Rollback**:
```bash
ssh user@staging-host
cd /opt/vigil
docker-compose down
git checkout previous-commit
docker-compose up -d
```

### Production Deployment

**Triggered by version tag** (e.g., `v1.2.3`):

1. Create GitHub release with changelog
2. Build production images with version tags
3. Security scan production images
4. Backup current deployment
5. Deploy to production VMs
6. Run smoke tests
7. Auto-rollback on failure
8. Slack notification

**Creating a Release**:
```bash
# Tag the release
git tag -a v1.2.3 -m "Release version 1.2.3"
git push origin v1.2.3

# GitHub Actions will automatically:
# 1. Build images
# 2. Create release
# 3. Deploy to production
```

### Rollback Procedure

**Automatic Rollback** (on health check failure):
```yaml
- name: Rollback on failure
  if: failure()
  run: |
    ssh $VM_USER@$VM_HOST '
      cd /opt/vigil &&
      docker-compose down &&
      docker-compose up -d --force-recreate
    '
```

**Manual Rollback**:
```bash
# SSH to production
ssh prod-user@prod-host

# View available images
docker images | grep vigil

# Rollback to previous version
cd /opt/vigil
export IMAGE_TAG=v1.2.2  # Previous version
docker-compose up -d --force-recreate
```

---

## Performance Optimization

### Caching Strategy

**Docker Layer Caching**:
```yaml
- name: Build and push
  uses: docker/build-push-action@v5
  with:
    cache-from: type=gha
    cache-to: type=gha,mode=max
```

**Dependency Caching**:
```yaml
- name: Cache pip dependencies
  uses: actions/cache@v3
  with:
    path: ~/.cache/pip
    key: ${{ runner.os }}-pip-${{ hashFiles('requirements.txt') }}
```

**NPM Caching**:
```yaml
- name: Setup Node.js
  uses: actions/setup-node@v4
  with:
    cache: 'npm'
    cache-dependency-path: frontend/package-lock.json
```

### Parallel Execution

Jobs run in parallel when possible:
- All linting jobs run concurrently
- Unit tests (backend/frontend) run concurrently
- Security scans run in parallel

**Estimated Pipeline Duration**:
- Linting: 2-3 minutes
- Tests: 5-7 minutes
- Build: 8-10 minutes
- Deploy: 3-5 minutes
- **Total**: ~15 minutes (with caching)

---

## Monitoring & Alerts

### Deployment Notifications

**Slack Integration**:
```yaml
- name: Notify Slack
  uses: slackapi/slack-github-action@v1
  with:
    payload: |
      {
        "text": "Deployment Successful",
        "blocks": [
          {
            "type": "section",
            "text": {
              "type": "mrkdwn",
              "text": "*Version:* ${{ github.ref_name }}"
            }
          }
        ]
      }
```

**Notification Triggers**:
- Staging deployment success/failure
- Production deployment success/failure
- Nightly test failures
- Security scan failures

### GitHub Status Checks

**Required Checks** (configure in branch protection):
- lint-backend
- lint-frontend
- test-unit-backend
- test-integration
- security-scan-python

**Branch Protection Rules**:
```
Settings → Branches → Branch protection rules
✓ Require status checks to pass before merging
✓ Require branches to be up to date before merging
✓ Require linear history
```

### Sentry Integration

**Error Tracking**:
- Automatic error capture in production
- Performance monitoring
- Release tracking

**Configuration**:
```python
# backend/monitoring.py
sentry_sdk.init(
    dsn=os.getenv("SENTRY_DSN"),
    environment=os.getenv("ENVIRONMENT"),
    release=os.getenv("RELEASE_VERSION")
)
```

---

## Troubleshooting

### Pipeline Failures

**Build Failures**:
```bash
# Check logs
gh run view --log

# Rerun failed jobs
gh run rerun --failed
```

**Test Failures**:
```bash
# Run tests locally
pytest tests/unit/ -v --lf  # Run last failed

# Check test logs in GitHub Actions
Actions → Failed workflow → test-unit-backend → Logs
```

**Deployment Failures**:
```bash
# SSH to VM and check logs
ssh user@host
cd /opt/vigil
docker-compose logs --tail=100 backend

# Check service status
docker-compose ps
```

### Common Issues

**Issue**: Cache not working
**Solution**: Clear cache and rerun workflow

**Issue**: Docker build timeout
**Solution**: Optimize Dockerfile, reduce image size

**Issue**: Test database connection failed
**Solution**: Ensure PostgreSQL service is healthy in workflow

**Issue**: Permission denied on deployment
**Solution**: Check SSH key is correct and VM user has docker permissions

---

## Best Practices

### 1. Semantic Versioning

Use semantic versioning for releases:
- **v1.0.0**: Major release
- **v1.1.0**: Minor feature addition
- **v1.1.1**: Bug fix/patch

### 2. Commit Messages

Follow conventional commits:
```
feat: Add new case search functionality
fix: Resolve authentication token expiry issue
docs: Update API documentation
test: Add unit tests for Claude service
ci: Optimize Docker build caching
```

### 3. Pull Request Workflow

1. Create feature branch from `develop`
2. Make changes and commit
3. Push and create PR to `develop`
4. Wait for CI checks to pass
5. Request code review
6. Merge to `develop`
7. Periodic merges from `develop` to `main`
8. Tag releases from `main`

### 4. Emergency Hotfixes

```bash
# Create hotfix branch from main
git checkout main
git checkout -b hotfix/critical-bug

# Make fix and test locally
pytest tests/

# Commit and push
git commit -m "fix: Resolve critical bug"
git push origin hotfix/critical-bug

# Create PR directly to main
# After merge, tag new patch version
git tag v1.2.4
git push origin v1.2.4
```

---

## Additional Resources

- [GitHub Actions Documentation](https://docs.github.com/en/actions)
- [Docker Best Practices](https://docs.docker.com/develop/dev-best-practices/)
- [Semantic Versioning](https://semver.org/)
- [Conventional Commits](https://www.conventionalcommits.org/)

