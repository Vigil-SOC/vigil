#!/bin/bash

# Run all tests with detailed output
# This script can be used locally and in CI/CD

# Don't exit on error - we want to run all tests and report failures
set +e

# Get the script directory and project root
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"

# Set paths for Python and pytest
# Check multiple possible venv locations
if [ -f "$PROJECT_ROOT/venv/bin/pytest" ]; then
    PYTEST_BIN="$PROJECT_ROOT/venv/bin/pytest"
elif [ -f "/Users/mando222/Github/deeptempo-ai-soc/venv/bin/pytest" ]; then
    PYTEST_BIN="/Users/mando222/Github/deeptempo-ai-soc/venv/bin/pytest"
elif command -v pytest &> /dev/null; then
    PYTEST_BIN="pytest"
else
    PYTEST_BIN=""
fi

echo "=================================================="
echo "Running Vigil-SOC Test Suite"
echo "=================================================="
echo ""

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# Track failures
BACKEND_FAILED=0
FRONTEND_FAILED=0

# Function to print section headers
print_section() {
    echo ""
    echo "=================================================="
    echo "$1"
    echo "=================================================="
    echo ""
}

# Backend Tests
print_section "BACKEND TESTS"

if [ -d "tests" ]; then
    echo "Running Python backend tests..."
    if [ -n "$PYTEST_BIN" ] && [ -f "$PYTEST_BIN" ]; then
        "$PYTEST_BIN" tests/ -v --tb=short --color=yes 2>&1 | tee backend-test-output.log
        BACKEND_EXIT_CODE=${PIPESTATUS[0]}
        if [ $BACKEND_EXIT_CODE -eq 0 ]; then
            echo -e "${GREEN}✓ Backend tests passed${NC}"
        else
            echo -e "${RED}✗ Backend tests failed (exit code: $BACKEND_EXIT_CODE)${NC}"
            BACKEND_FAILED=1
        fi
    elif [ -n "$PYTEST_BIN" ]; then
        # pytest command exists but not as a file (e.g., in PATH)
        "$PYTEST_BIN" tests/ -v --tb=short --color=yes 2>&1 | tee backend-test-output.log
        BACKEND_EXIT_CODE=${PIPESTATUS[0]}
        if [ $BACKEND_EXIT_CODE -eq 0 ]; then
            echo -e "${GREEN}✓ Backend tests passed${NC}"
        else
            echo -e "${RED}✗ Backend tests failed (exit code: $BACKEND_EXIT_CODE)${NC}"
            BACKEND_FAILED=1
        fi
    else
        echo -e "${RED}✗ pytest not found${NC}"
        echo -e "${YELLOW}Install with: pip install -r requirements.txt${NC}"
        BACKEND_FAILED=1
    fi
else
    echo -e "${YELLOW}⚠ No backend tests directory found${NC}"
fi

# Frontend Tests
print_section "FRONTEND TESTS"

if [ -d "frontend" ]; then
    cd frontend
    echo "Running frontend tests..."
    npm test -- --run --reporter=verbose 2>&1 | tee ../frontend-test-output.log
    FRONTEND_EXIT_CODE=${PIPESTATUS[0]}
    if [ $FRONTEND_EXIT_CODE -eq 0 ]; then
        echo -e "${GREEN}✓ Frontend tests passed${NC}"
    else
        echo -e "${RED}✗ Frontend tests failed (exit code: $FRONTEND_EXIT_CODE)${NC}"
        FRONTEND_FAILED=1
    fi
    cd ..
else
    echo -e "${YELLOW}⚠ No frontend directory found${NC}"
fi

# Summary
print_section "TEST SUMMARY"

if [ $BACKEND_FAILED -eq 0 ] && [ $FRONTEND_FAILED -eq 0 ]; then
    echo -e "${GREEN}✓ ALL TESTS PASSED${NC}"
    echo ""
    echo "Test output saved to:"
    echo "  - backend-test-output.log"
    echo "  - frontend-test-output.log"
    exit 0
else
    echo -e "${RED}✗ SOME TESTS FAILED${NC}"
    echo ""
    if [ $BACKEND_FAILED -eq 1 ]; then
        echo -e "${RED}  - Backend tests failed${NC}"
    fi
    if [ $FRONTEND_FAILED -eq 1 ]; then
        echo -e "${RED}  - Frontend tests failed${NC}"
    fi
    echo ""
    echo "See detailed output in:"
    echo "  - backend-test-output.log"
    echo "  - frontend-test-output.log"
    exit 1
fi

