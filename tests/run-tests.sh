#!/usr/bin/env bash
# tests/run-tests.sh — run the full Vigil test suite (pytest + vitest).
#
# Usable locally and in CI. Runs every suite to completion and reports all
# failures at the end rather than aborting on the first one.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/lib.sh
source "$SCRIPT_DIR/../scripts/lib.sh"
# lib.sh enables `set -euo pipefail`; relax it here so a failing suite doesn't
# abort the run before the other suites (and the summary) get a chance.
set +eu

# Resolve pytest: prefer the project venv, then a pytest on PATH, then run it
# via a discovered Python 3.10+ interpreter.
if [ -x "$REPO_ROOT/venv/bin/pytest" ]; then
    PYTEST="$REPO_ROOT/venv/bin/pytest"
elif command -v pytest &>/dev/null; then
    PYTEST="pytest"
elif PY="$(find_python 2>/dev/null)"; then
    PYTEST="$PY -m pytest"
else
    PYTEST=""
fi

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# Track failures
BACKEND_FAILED=0
FRONTEND_FAILED=0

print_section() {
    echo ""
    echo "=================================================="
    echo "$1"
    echo "=================================================="
    echo ""
}

echo "=================================================="
echo "Running Vigil-SOC Test Suite"
echo "=================================================="

# Backend Tests
print_section "BACKEND TESTS"

if [ -d "$REPO_ROOT/tests" ]; then
    if [ -n "$PYTEST" ]; then
        echo "Running Python backend tests..."
        # shellcheck disable=SC2086  # $PYTEST may be "python -m pytest"
        $PYTEST "$REPO_ROOT/tests/" -v --tb=short --color=yes 2>&1 \
            | tee "$REPO_ROOT/backend-test-output.log"
        BACKEND_EXIT_CODE=${PIPESTATUS[0]}
        if [ "$BACKEND_EXIT_CODE" -eq 0 ]; then
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

if [ -d "$REPO_ROOT/frontend" ]; then
    echo "Running frontend tests..."
    ( cd "$REPO_ROOT/frontend" && npm run test:run ) 2>&1 \
        | tee "$REPO_ROOT/frontend-test-output.log"
    FRONTEND_EXIT_CODE=${PIPESTATUS[0]}
    if [ "$FRONTEND_EXIT_CODE" -eq 0 ]; then
        echo -e "${GREEN}✓ Frontend tests passed${NC}"
    else
        echo -e "${RED}✗ Frontend tests failed (exit code: $FRONTEND_EXIT_CODE)${NC}"
        FRONTEND_FAILED=1
    fi
else
    echo -e "${YELLOW}⚠ No frontend directory found${NC}"
fi

# Summary
print_section "TEST SUMMARY"

echo "Test output saved to:"
echo "  - backend-test-output.log"
echo "  - frontend-test-output.log"
echo ""

if [ "$BACKEND_FAILED" -eq 0 ] && [ "$FRONTEND_FAILED" -eq 0 ]; then
    echo -e "${GREEN}✓ ALL TESTS PASSED${NC}"
    exit 0
else
    echo -e "${RED}✗ SOME TESTS FAILED${NC}"
    [ "$BACKEND_FAILED" -eq 1 ] && echo -e "${RED}  - Backend tests failed${NC}"
    [ "$FRONTEND_FAILED" -eq 1 ] && echo -e "${RED}  - Frontend tests failed${NC}"
    exit 1
fi
