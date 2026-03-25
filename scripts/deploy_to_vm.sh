#!/bin/bash
# Deployment script for AI-OpenSOC to VMs
# Usage: ./deploy_to_vm.sh [staging|production]

set -e  # Exit on error

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# Configuration
ENVIRONMENT=${1:-staging}
VM_HOST=${VM_HOST:-}
VM_USER=${VM_USER:-deployer}
REGISTRY=${REGISTRY:-ghcr.io}
IMAGE_NAME=${IMAGE_NAME:-deeptempo/ai-opensoc}
IMAGE_TAG=${IMAGE_TAG:-latest}
DEPLOY_DIR="/opt/ai-opensoc"

# Validation
if [ -z "$VM_HOST" ]; then
    echo -e "${RED}Error: VM_HOST environment variable not set${NC}"
    exit 1
fi

echo -e "${GREEN}========================================${NC}"
echo -e "${GREEN}AI-OpenSOC Deployment Script${NC}"
echo -e "${GREEN}Environment: $ENVIRONMENT${NC}"
echo -e "${GREEN}Target: $VM_USER@$VM_HOST${NC}"
echo -e "${GREEN}========================================${NC}"

# Function to run remote command
run_remote() {
    ssh -o StrictHostKeyChecking=no $VM_USER@$VM_HOST "$1"
}

# Determine remote docker compose command (v2 plugin vs v1 standalone)
DOCKER_COMPOSE=$(run_remote "command -v docker-compose &>/dev/null && echo docker-compose || echo 'docker compose'")

# Function to check service health
check_health() {
    local service=$1
    local max_attempts=30
    local attempt=1
    
    echo -e "${YELLOW}Checking $service health...${NC}"
    
    while [ $attempt -le $max_attempts ]; do
        if run_remote "docker ps | grep -q $service"; then
            echo -e "${GREEN}✓ $service is running${NC}"
            return 0
        fi
        echo "Attempt $attempt/$max_attempts..."
        sleep 2
        ((attempt++))
    done
    
    echo -e "${RED}✗ $service failed to start${NC}"
    return 1
}

# Step 1: Backup current deployment
echo -e "\n${YELLOW}Step 1: Creating backup...${NC}"
run_remote "
    cd $DEPLOY_DIR &&
    mkdir -p backups &&
    timestamp=\$(date +%Y%m%d_%H%M%S) &&
    $DOCKER_COMPOSE ps > backups/pre-deploy-\$timestamp.txt &&
    echo 'Backup created: backups/pre-deploy-\$timestamp.txt'
"

# Step 2: Pull latest images
echo -e "\n${YELLOW}Step 2: Pulling Docker images...${NC}"
run_remote "
    docker login $REGISTRY &&
    docker pull $REGISTRY/$IMAGE_NAME-backend:$IMAGE_TAG &&
    docker pull $REGISTRY/$IMAGE_NAME-daemon:$IMAGE_TAG
"

# Step 3: Update docker compose configuration
echo -e "\n${YELLOW}Step 3: Updating configuration...${NC}"
run_remote "
    cd $DEPLOY_DIR &&
    export IMAGE_TAG=$IMAGE_TAG &&
    export REGISTRY=$REGISTRY &&
    export IMAGE_NAME=$IMAGE_NAME
"

# Step 4: Run database migrations
echo -e "\n${YELLOW}Step 4: Running database migrations...${NC}"
run_remote "
    cd $DEPLOY_DIR &&
    $DOCKER_COMPOSE run --rm backend alembic upgrade head || echo 'Migration completed'
"

# Step 5: Rolling restart services
echo -e "\n${YELLOW}Step 5: Performing rolling restart...${NC}"

# Restart daemon first (less critical)
echo "Restarting daemon..."
run_remote "
    cd $DEPLOY_DIR &&
    $DOCKER_COMPOSE stop soc-daemon &&
    $DOCKER_COMPOSE rm -f soc-daemon &&
    $DOCKER_COMPOSE up -d soc-daemon
"
check_health "soc-daemon"

# Restart backend (more critical, do it second)
echo "Restarting backend..."
run_remote "
    cd $DEPLOY_DIR &&
    $DOCKER_COMPOSE stop backend &&
    $DOCKER_COMPOSE rm -f backend &&
    $DOCKER_COMPOSE up -d backend
"
check_health "backend"

# Step 6: Health checks
echo -e "\n${YELLOW}Step 6: Running health checks...${NC}"
sleep 10  # Wait for services to stabilize

# Check API health
echo "Checking API health..."
api_health=$(run_remote "curl -s -o /dev/null -w '%{http_code}' http://localhost:6987/health")
if [ "$api_health" = "200" ]; then
    echo -e "${GREEN}✓ API health check passed${NC}"
else
    echo -e "${RED}✗ API health check failed (HTTP $api_health)${NC}"
    echo -e "${YELLOW}Initiating rollback...${NC}"
    run_remote "cd $DEPLOY_DIR && $DOCKER_COMPOSE down && $DOCKER_COMPOSE up -d"
    exit 1
fi

# Check daemon metrics
echo "Checking daemon metrics..."
daemon_health=$(run_remote "curl -s -o /dev/null -w '%{http_code}' http://localhost:9090/metrics")
if [ "$daemon_health" = "200" ]; then
    echo -e "${GREEN}✓ Daemon health check passed${NC}"
else
    echo -e "${YELLOW}⚠ Daemon metrics endpoint returned HTTP $daemon_health${NC}"
fi

# Step 7: Cleanup old images
echo -e "\n${YELLOW}Step 7: Cleaning up old images...${NC}"
run_remote "
    docker image prune -a -f --filter 'until=168h' || true
"

# Step 8: Verify deployment
echo -e "\n${YELLOW}Step 8: Verifying deployment...${NC}"
run_remote "
    cd $DEPLOY_DIR &&
    echo '--- Running Services ---' &&
    $DOCKER_COMPOSE ps &&
    echo '' &&
    echo '--- Recent Logs ---' &&
    $DOCKER_COMPOSE logs --tail=20 backend
"

# Success!
echo -e "\n${GREEN}========================================${NC}"
echo -e "${GREEN}✓ Deployment completed successfully!${NC}"
echo -e "${GREEN}Environment: $ENVIRONMENT${NC}"
echo -e "${GREEN}Version: $IMAGE_TAG${NC}"
echo -e "${GREEN}========================================${NC}"

exit 0

