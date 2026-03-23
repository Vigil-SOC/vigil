#!/bin/bash
# Quick helper script to export PostgreSQL data to Splunk
# Usage: ./export_to_splunk.sh [options]

set -e

# Colors for output
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m' # No Color

# Default values
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON="${SCRIPT_DIR}/venv/bin/python"
EXPORT_SCRIPT="${SCRIPT_DIR}/scripts/export_postgres_to_splunk.py"

# Check if .env file exists
if [ ! -f "${SCRIPT_DIR}/.env" ]; then
    echo -e "${RED}Error: .env file not found${NC}"
    echo "Please create .env file with Splunk HEC configuration"
    exit 1
fi

# Load environment variables
source "${SCRIPT_DIR}/.env" 2>/dev/null || true

# Show banner
echo -e "${GREEN}╔══════════════════════════════════════════════════════════╗${NC}"
echo -e "${GREEN}║   PostgreSQL to Splunk Export Helper                    ║${NC}"
echo -e "${GREEN}╚══════════════════════════════════════════════════════════╝${NC}"
echo ""

# Check if SPLUNK_HEC_URL and SPLUNK_HEC_TOKEN are set
if [ -z "${SPLUNK_HEC_URL}" ] || [ -z "${SPLUNK_HEC_TOKEN}" ]; then
    echo -e "${YELLOW}Warning: SPLUNK_HEC_URL or SPLUNK_HEC_TOKEN not set in .env${NC}"
    echo ""
    echo "You can either:"
    echo "  1. Add to .env file:"
    echo "     SPLUNK_HEC_URL=https://your-splunk:8088/services/collector"
    echo "     SPLUNK_HEC_TOKEN=your-hec-token"
    echo "     SPLUNK_HEC_INDEX=deeptempo"
    echo ""
    echo "  2. Or pass them as command-line arguments"
    echo ""
    
    # Prompt for values
    read -p "Enter Splunk HEC URL (or press Enter to skip): " HEC_URL
    read -p "Enter Splunk HEC Token (or press Enter to skip): " HEC_TOKEN
    read -p "Enter Splunk Index [main]: " HEC_INDEX
    HEC_INDEX=${HEC_INDEX:-main}
    
    if [ -z "${HEC_URL}" ] || [ -z "${HEC_TOKEN}" ]; then
        echo ""
        echo -e "${YELLOW}Running with --save-to-file instead${NC}"
        OUTPUT_FILE="postgres_export_$(date +%Y%m%d_%H%M%S).json"
        $PYTHON "$EXPORT_SCRIPT" --save-to-file "$OUTPUT_FILE"
        exit 0
    fi
else
    HEC_URL="${SPLUNK_HEC_URL}"
    HEC_TOKEN="${SPLUNK_HEC_TOKEN}"
    HEC_INDEX="${SPLUNK_HEC_INDEX:-main}"
fi

# Show what will be exported
echo -e "${GREEN}Checking database...${NC}"
DB_INFO=$($PYTHON -c "
from database.connection import get_db_manager
from database.models import Finding, Case
from sqlalchemy import func

db_manager = get_db_manager()
db_manager.initialize()
db = db_manager.get_session()

finding_count = db.query(func.count(Finding.finding_id)).scalar()
case_count = db.query(func.count(Case.case_id)).scalar()

print(f'{finding_count},{case_count}')
")

IFS=',' read -r FINDING_COUNT CASE_COUNT <<< "$DB_INFO"

echo ""
echo "Database Status:"
echo "  Findings: ${FINDING_COUNT} total → Will export $((FINDING_COUNT / 2))"
echo "  Cases:    ${CASE_COUNT} total → Will export $((CASE_COUNT / 2))"
echo ""
echo "Export Configuration:"
echo "  HEC URL:  ${HEC_URL}"
echo "  Index:    ${HEC_INDEX}"
echo ""

# Ask for confirmation
read -p "Continue with export? (y/N): " CONFIRM
if [[ ! $CONFIRM =~ ^[Yy]$ ]]; then
    echo "Export cancelled"
    exit 0
fi

# Run the export
echo ""
echo -e "${GREEN}Starting export...${NC}"
echo ""

$PYTHON "$EXPORT_SCRIPT" \
    --hec-url "${HEC_URL}" \
    --hec-token "${HEC_TOKEN}" \
    --index "${HEC_INDEX}" \
    --no-verify-ssl

if [ $? -eq 0 ]; then
    echo ""
    echo -e "${GREEN}╔══════════════════════════════════════════════════════════╗${NC}"
    echo -e "${GREEN}║   Export Completed Successfully! ✓                       ║${NC}"
    echo -e "${GREEN}╚══════════════════════════════════════════════════════════╝${NC}"
    echo ""
    echo "View in Splunk:"
    echo "  index=${HEC_INDEX} source=\"postgresql_export\""
    echo ""
else
    echo ""
    echo -e "${RED}Export failed. Check the error messages above.${NC}"
    exit 1
fi

