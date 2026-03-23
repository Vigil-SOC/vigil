#!/bin/bash
# Start PostgreSQL database for Vigil SOC v2.0

set -e

echo "=================================================="
echo "Vigil SOC v2.0 - Database Startup"
echo "=================================================="

# Check Docker
if ! command -v docker &> /dev/null; then
    echo "❌ Docker not installed"
    echo "   Install from: https://www.docker.com/get-started"
    exit 1
fi

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

# Load env vars
if [ -f .env ]; then
    echo "✓ Loading .env"
    export $(cat .env | grep -v '^#' | xargs)
else
    echo "⚠️  No .env file, using defaults"
fi

# Start PostgreSQL
echo ""
echo "Starting PostgreSQL..."
cd docker
$DOCKER_COMPOSE up -d postgres
cd ..

# Wait for ready
echo ""
echo "Waiting for PostgreSQL..."
for i in {1..30}; do
    if docker exec deeptempo-postgres pg_isready -U postgres -d deeptempo &> /dev/null; then
        echo "✓ PostgreSQL ready!"
        break
    fi
    if [ $i -eq 30 ]; then
        echo "❌ PostgreSQL failed to start in 30s"
        echo "   Check logs: cd docker && $DOCKER_COMPOSE logs postgres"
        exit 1
    fi
    echo -n "."
    sleep 1
done

# Initialize database
echo ""
echo "Initializing database tables..."
if [ -f "venv/bin/activate" ]; then
    source venv/bin/activate
    python3 -c "
from database.connection import init_database
try:
    init_database(create_tables=True)
    print('✓ Database tables created')
except Exception as e:
    print(f'⚠️  Database init warning: {e}')
" || echo "⚠️  Could not initialize tables (may already exist)"
else
    echo "⚠️  No venv found. Tables may need manual creation."
fi

# Show status
echo ""
echo "=================================================="
echo "✓ Database Running!"
echo "=================================================="
echo "PostgreSQL: localhost:5432"
echo "Database:   deeptempo"
echo "User:       postgres"
echo ""
echo "Useful commands:"
echo "  View logs:      cd docker && $DOCKER_COMPOSE logs -f postgres"
echo "  Stop database:  cd docker && $DOCKER_COMPOSE stop postgres"
echo "  Connect:        docker exec -it deeptempo-postgres psql -U postgres -d deeptempo"
echo ""

