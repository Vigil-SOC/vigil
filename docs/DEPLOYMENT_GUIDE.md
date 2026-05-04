# Deployment Guide

Complete guide to deploying Vigil to VMs and managing production environments.

## Table of Contents

- [Overview](#overview)
- [VM Requirements](#vm-requirements)
- [Initial Setup](#initial-setup)
- [Deployment Methods](#deployment-methods)
- [Configuration](#configuration)
- [Monitoring](#monitoring)
- [Maintenance](#maintenance)
- [Troubleshooting](#troubleshooting)

---

## Overview

Vigil can be deployed in several configurations:

1. **Single VM**: All services on one machine (development/testing)
2. **Multi-VM**: Distributed deployment (staging/production)
3. **Docker Compose**: Container-based deployment
4. **Kubernetes**: Orchestrated deployment (future)

This guide focuses on **VM deployment with Docker Compose**.

---

## VM Requirements

### Minimum Requirements (Single VM)

- **OS**: Ubuntu 22.04 LTS
- **CPU**: 4 cores
- **RAM**: 8 GB
- **Disk**: 50 GB SSD
- **Network**: Public IP with firewall

### Recommended Requirements (Production)

- **OS**: Ubuntu 22.04 LTS
- **CPU**: 8 cores
- **RAM**: 16 GB
- **Disk**: 100 GB SSD
- **Network**: Load balancer + multiple VMs

### Multi-VM Architecture

**Recommended Production Setup**:

```
┌─────────────────┐
│ Load Balancer   │
│  (Nginx/HAProxy)│
└────────┬────────┘
         │
    ┌────┴────┐
    │         │
┌───▼────┐ ┌─▼──────┐
│ VM 1   │ │ VM 2   │
│Backend │ │Backend │
└────────┘ └────────┘
    │         │
    └────┬────┘
         │
    ┌────▼────┐
    │ VM 3    │
    │Postgres │
    └────┬────┘
         │
    ┌────▼────┐
    │ VM 4    │
    │ Daemon  │
    └─────────┘
```

**VM Roles**:
- **VM 1-2**: Backend API (load balanced)
- **VM 3**: PostgreSQL database (with replication)
- **VM 4**: SOC Daemon (autonomous operations)

---

## Initial Setup

### 1. Prepare VM

```bash
# Update system
sudo apt update && sudo apt upgrade -y

# Install Docker
curl -fsSL https://get.docker.com -o get-docker.sh
sudo sh get-docker.sh
sudo usermod -aG docker $USER

# Install Docker Compose
sudo curl -L "https://github.com/docker/compose/releases/latest/download/docker-compose-$(uname -s)-$(uname -m)" -o /usr/local/bin/docker-compose
sudo chmod +x /usr/local/bin/docker-compose

# Verify installation
docker --version
docker-compose --version

# Logout and login again for group changes
```

### 2. Create Deployment User

```bash
# Create deployer user
sudo adduser deployer
sudo usermod -aG docker deployer
sudo usermod -aG sudo deployer

# Setup SSH key for deployer
sudo su - deployer
mkdir ~/.ssh
chmod 700 ~/.ssh
vi ~/.ssh/authorized_keys  # Paste GitHub Actions public key
chmod 600 ~/.ssh/authorized_keys
```

### 3. Prepare Deployment Directory

```bash
# As deployer user
sudo mkdir -p /opt/vigil
sudo chown deployer:deployer /opt/vigil
cd /opt/vigil

# Clone repository
git clone https://github.com/your-org/vigil.git .

# Create necessary directories
mkdir -p logs evidence backups
```

### 4. Configure Firewall

```bash
# Allow SSH
sudo ufw allow 22/tcp

# Allow API
sudo ufw allow 6987/tcp

# Allow Frontend
sudo ufw allow 6988/tcp

# Allow Prometheus metrics
sudo ufw allow 9090/tcp

# Allow Webhook ingestion
sudo ufw allow 8081/tcp

# Enable firewall
sudo ufw enable
sudo ufw status
```

---

## Deployment Methods

### Method 1: Automated Deployment (CI/CD)

**Prerequisites**:
- GitHub Actions configured
- SSH keys added to secrets
- VM accessible from GitHub

**Staging Deployment**:
```bash
# Push to main branch
git push origin main

# GitHub Actions automatically:
# 1. Runs tests
# 2. Builds images
# 3. Deploys to staging
```

**Production Deployment**:
```bash
# Tag a release
git tag -a v1.2.3 -m "Release version 1.2.3"
git push origin v1.2.3

# GitHub Actions automatically:
# 1. Creates release
# 2. Builds production images
# 3. Deploys to production
# 4. Runs health checks
```

### Method 2: Manual Deployment

```bash
# On deployment machine
cd /opt/vigil

# Pull latest code
git pull origin main

# Set environment variables
export REGISTRY=ghcr.io
export IMAGE_NAME=your-org/vigil
export IMAGE_TAG=latest

# Run deployment script
chmod +x scripts/deploy_to_vm.sh
./scripts/deploy_to_vm.sh production
```

### Method 3: Docker Compose Direct

```bash
# On deployment machine
cd /opt/vigil

# Set environment variables in .env file
cp env.example .env
vi .env  # Edit configuration

# Pull images
docker-compose pull

# Start services
docker-compose up -d

# Check status
docker-compose ps
```

---

## Configuration

### Environment Variables

**File**: `/opt/vigil/.env`

```bash
# Database
DATABASE_URL=postgresql://deeptempo:secure_password@postgres:5432/deeptempo_soc
POSTGRES_PASSWORD=secure_password_change_me

# API Keys
ANTHROPIC_API_KEY=sk-ant-your-key-here

# Backend
SECRET_KEY=your-secret-key-here
ENVIRONMENT=production

# Monitoring
SENTRY_DSN=https://your-sentry-dsn
RELEASE_VERSION=v1.2.3

# SIEM Integrations
SPLUNK_URL=https://splunk.example.com:8089
SPLUNK_USERNAME=admin
SPLUNK_PASSWORD=splunk_password

# External Services
SLACK_BOT_TOKEN=xoxb-your-token
SLACK_DEFAULT_CHANNEL=#soc-alerts
```

### Docker Compose Configuration

**File**: `/opt/vigil/docker-compose.yml`

```yaml
version: '3.8'

services:
  postgres:
    image: postgres:16-alpine
    container_name: deeptempo-postgres
    environment:
      POSTGRES_DB: deeptempo_soc
      POSTGRES_USER: deeptempo
      POSTGRES_PASSWORD: ${POSTGRES_PASSWORD}
    volumes:
      - postgres_data:/var/lib/postgresql/data
    restart: unless-stopped

  backend:
    image: ${REGISTRY}/${IMAGE_NAME}-backend:${IMAGE_TAG}
    container_name: deeptempo-backend
    environment:
      - DATABASE_URL
      - ANTHROPIC_API_KEY
      - SECRET_KEY
      - SENTRY_DSN
    ports:
      - "6987:6987"
    depends_on:
      - postgres
    restart: unless-stopped

  soc-daemon:
    image: ${REGISTRY}/${IMAGE_NAME}-daemon:${IMAGE_TAG}
    container_name: deeptempo-daemon
    environment:
      - DATABASE_URL
      - ANTHROPIC_API_KEY
    ports:
      - "8081:8081"  # Webhook
      - "9090:9090"  # Metrics
    depends_on:
      - postgres
    restart: unless-stopped

volumes:
  postgres_data:
```

### Nginx Reverse Proxy (Optional)

**File**: `/etc/nginx/sites-available/vigil`

```nginx
server {
    listen 80;
    server_name app.deeptempo.ai;
    
    # Redirect to HTTPS
    return 301 https://$server_name$request_uri;
}

server {
    listen 443 ssl http2;
    server_name app.deeptempo.ai;
    
    # SSL certificates
    ssl_certificate /etc/letsencrypt/live/app.deeptempo.ai/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/app.deeptempo.ai/privkey.pem;
    
    # API
    location /api {
        proxy_pass http://localhost:6987;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
    
    # Frontend
    location / {
        root /opt/vigil/frontend/build;
        try_files $uri $uri/ /index.html;
    }
}
```

---

## Monitoring

### Health Checks

```bash
# API health
curl http://localhost:6987/health

# Daemon metrics
curl http://localhost:9090/metrics

# Docker container status
docker-compose ps

# View logs
docker-compose logs -f backend
docker-compose logs -f soc-daemon
```

### Log Management

**View Logs**:
```bash
# All services
docker-compose logs --tail=100

# Specific service
docker-compose logs -f backend

# Save logs to file
docker-compose logs > logs/deployment-$(date +%Y%m%d).log
```

**Log Rotation**:
```json
// /etc/docker/daemon.json
{
  "log-driver": "json-file",
  "log-opts": {
    "max-size": "10m",
    "max-file": "3"
  }
}
```

### Metrics Collection

**Prometheus Metrics**:
```bash
# Access metrics endpoint
curl http://localhost:9090/metrics

# Example metrics:
# - http_requests_total
# - http_request_duration_seconds
# - active_cases_total
# - findings_processed_total
```

**Grafana Dashboard** (Optional):
```bash
# Install Grafana
docker run -d -p 3000:3000 grafana/grafana

# Add Prometheus datasource
# Import Vigil dashboard
```

---

## Maintenance

### Database Backups

**Automated Daily Backups**:
```bash
# Create backup script
vi /opt/vigil/scripts/backup.sh
```

```bash
#!/bin/bash
BACKUP_DIR="/opt/vigil/backups"
DATE=$(date +%Y%m%d_%H%M%S)
BACKUP_FILE="$BACKUP_DIR/deeptempo_$DATE.sql"

# Create backup
docker-compose exec -T postgres pg_dump -U deeptempo deeptempo_soc > $BACKUP_FILE

# Compress
gzip $BACKUP_FILE

# Remove backups older than 30 days
find $BACKUP_DIR -name "*.sql.gz" -mtime +30 -delete

echo "Backup completed: ${BACKUP_FILE}.gz"
```

**Schedule with Cron**:
```bash
# Edit crontab
crontab -e

# Add daily backup at 2 AM
0 2 * * * /opt/vigil/scripts/backup.sh >> /opt/vigil/logs/backup.log 2>&1
```

### Database Restore

```bash
# Stop services
docker-compose stop backend soc-daemon

# Restore from backup
gunzip -c backups/deeptempo_20260127.sql.gz | docker-compose exec -T postgres psql -U deeptempo deeptempo_soc

# Start services
docker-compose start backend soc-daemon
```

### Update Deployment

```bash
# Pull latest images
docker-compose pull

# Recreate containers
docker-compose up -d --force-recreate

# Verify
docker-compose ps
```

### Certificate Renewal (Let's Encrypt)

```bash
# Renew certificates
sudo certbot renew --nginx

# Verify renewal
sudo certbot certificates

# Add to cron for auto-renewal
0 0 1 * * certbot renew --nginx >> /var/log/letsencrypt/renew.log 2>&1
```

---

## Troubleshooting

### Service Not Starting

```bash
# Check logs
docker-compose logs backend

# Check configuration
docker-compose config

# Restart service
docker-compose restart backend

# Rebuild and restart
docker-compose up -d --build --force-recreate backend
```

### Database Connection Issues

```bash
# Check PostgreSQL status
docker-compose ps postgres

# Test connection
docker-compose exec postgres psql -U deeptempo -d deeptempo_soc -c "SELECT 1;"

# Check network
docker network ls
docker network inspect vigil_default
```

### High Memory Usage

```bash
# Check resource usage
docker stats

# Restart memory-heavy service
docker-compose restart soc-daemon

# Increase Docker memory limit
# Edit /etc/docker/daemon.json
{
  "default-ulimits": {
    "memlock": {
      "soft": -1,
      "hard": -1
    }
  }
}
```

### Disk Space Issues

```bash
# Check disk usage
df -h

# Clean Docker resources
docker system prune -a --volumes

# Remove old images
docker images | grep vigil | grep -v latest | awk '{print $3}' | xargs docker rmi

# Cleanup old logs
find /opt/vigil/logs -name "*.log" -mtime +7 -delete
```

---

## Security Hardening

### 1. Secure SSH

```bash
# Disable root login
sudo vi /etc/ssh/sshd_config
# Set: PermitRootLogin no
# Set: PasswordAuthentication no

# Restart SSH
sudo systemctl restart sshd
```

### 2. Enable Fail2Ban

```bash
# Install fail2ban
sudo apt install fail2ban

# Configure
sudo vi /etc/fail2ban/jail.local
# Add SSH protection
sudo systemctl enable fail2ban
sudo systemctl start fail2ban
```

### 3. Database Security

```bash
# Use strong passwords
# Enable SSL for PostgreSQL connections
# Restrict PostgreSQL to localhost only
```

### 4. API Security

```bash
# Use HTTPS only
# Enable rate limiting
# Configure CORS properly
# Use secure session cookies
```

---

## Rollback Procedures

### Quick Rollback

```bash
# Stop current deployment
docker-compose down

# Pull previous version
export IMAGE_TAG=v1.2.2
docker-compose pull
docker-compose up -d

# Verify
curl http://localhost:6987/health
```

### Complete Rollback with Database

```bash
# 1. Stop services
docker-compose down

# 2. Restore database backup
gunzip -c backups/pre-v1.2.3.sql.gz | docker-compose exec -T postgres psql -U deeptempo deeptempo_soc

# 3. Revert code
git checkout v1.2.2

# 4. Deploy previous version
export IMAGE_TAG=v1.2.2
docker-compose up -d

# 5. Verify
docker-compose ps
curl http://localhost:6987/health
```

---

## Best Practices

1. **Always test in staging first**
2. **Backup before major updates**
3. **Monitor logs during deployment**
4. **Keep rollback images available**
5. **Document all configuration changes**
6. **Use infrastructure as code (IaC)**
7. **Automate routine maintenance**
8. **Set up alerts for critical issues**

---

## Additional Resources

- [Docker Documentation](https://docs.docker.com/)
- [PostgreSQL Documentation](https://www.postgresql.org/docs/)
- [Nginx Documentation](https://nginx.org/en/docs/)
- [Let's Encrypt](https://letsencrypt.org/)
- [Sentry Documentation](https://docs.sentry.io/)

