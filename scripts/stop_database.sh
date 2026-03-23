#!/bin/bash
# Stop PostgreSQL database for Vigil SOC v2.0

set -e

echo "=================================================="
echo "Vigil SOC v2.0 - Database Shutdown"
echo "=================================================="

# Check Docker Compose
if ! command -v docker-compose &> /dev/null && ! docker compose version &> /dev/null; then
    echo "❌ Docker Compose not installed"
    exit 1
fi

# Determine compose command
if command -v docker-compose &> /dev/null; then
    DOCKER_COMPOSE="docker-compose"
else
    DOCKER_COMPOSE="docker compose"
fi

# Stop containers
echo "Stopping PostgreSQL..."
cd docker
$DOCKER_COMPOSE stop postgres
cd ..

echo ""
echo "✓ Database stopped"
echo ""
echo "To start: ./scripts/start_database.sh"
echo "To remove: cd docker && $DOCKER_COMPOSE down -v"
echo ""

