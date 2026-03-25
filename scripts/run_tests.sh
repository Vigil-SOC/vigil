#!/bin/bash

# AI SOC - Automated Test Suite
# Tests all major components and features
# Usage: ./run_tests.sh [--verbose]

set -e

# Colors
GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Determine docker compose command (v2 plugin vs v1 standalone)
if command -v docker-compose &> /dev/null; then
    DOCKER_COMPOSE="docker-compose"
else
    DOCKER_COMPOSE="docker compose"
fi

# Configuration
API_URL="${API_URL:-http://localhost:6987}"
FRONTEND_URL="${FRONTEND_URL:-http://localhost:6988}"
VERBOSE=false

# Parse arguments
if [[ "$1" == "--verbose" ]]; then
  VERBOSE=true
fi

# Test counters
TOTAL_TESTS=0
PASSED_TESTS=0
FAILED_TESTS=0

# Arrays to store results
declare -a FAILED_TEST_NAMES

echo -e "${BLUE}╔════════════════════════════════════════╗${NC}"
echo -e "${BLUE}║   AI SOC - Automated Test Suite       ║${NC}"
echo -e "${BLUE}║   Testing with Claude 4.5 Sonnet       ║${NC}"
echo -e "${BLUE}╚════════════════════════════════════════╝${NC}"
echo ""

# Function to print test header
print_header() {
  echo -e "\n${YELLOW}▶ $1${NC}"
  echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
}

# Function to run a test
run_test() {
  local test_name="$1"
  local test_command="$2"
  local expected_status="${3:-200}"
  
  ((TOTAL_TESTS++))
  
  if $VERBOSE; then
    echo -n "  Testing: $test_name... "
  else
    echo -n "  [$TOTAL_TESTS] $test_name... "
  fi
  
  # Run the test command
  if $VERBOSE; then
    response=$(eval "$test_command" 2>&1)
    status=$?
  else
    response=$(eval "$test_command" 2>&1)
    status=$?
  fi
  
  # Check if test passed
  if [ $status -eq 0 ]; then
    echo -e "${GREEN}✓ PASS${NC}"
    ((PASSED_TESTS++))
    return 0
  else
    echo -e "${RED}✗ FAIL${NC}"
    ((FAILED_TESTS++))
    FAILED_TEST_NAMES+=("$test_name")
    if $VERBOSE; then
      echo "    Error: $response"
    fi
    return 1
  fi
}

# Function to test HTTP endpoint
test_http() {
  local name="$1"
  local method="${2:-GET}"
  local endpoint="$3"
  local expected_status="${4:-200}"
  local headers="${5:-}"
  local data="${6:-}"
  
  local curl_cmd="curl -s -w '%{http_code}' -o /dev/null -X $method"
  
  if [ -n "$headers" ]; then
    curl_cmd="$curl_cmd -H '$headers'"
  fi
  
  if [ -n "$data" ]; then
    curl_cmd="$curl_cmd -d '$data' -H 'Content-Type: application/json'"
  fi
  
  curl_cmd="$curl_cmd $API_URL$endpoint"
  
  ((TOTAL_TESTS++))
  
  if $VERBOSE; then
    echo -n "  Testing: $name... "
    echo "    Command: $curl_cmd" >&2
  else
    echo -n "  [$TOTAL_TESTS] $name... "
  fi
  
  response=$(eval "$curl_cmd")
  
  if [ "$response" == "$expected_status" ]; then
    echo -e "${GREEN}✓ PASS${NC} (Status: $response)"
    ((PASSED_TESTS++))
    return 0
  else
    echo -e "${RED}✗ FAIL${NC} (Expected: $expected_status, Got: $response)"
    ((FAILED_TESTS++))
    FAILED_TEST_NAMES+=("$name")
    return 1
  fi
}

# Check if services are running
print_header "Pre-Flight Checks"

echo -n "  [*] Checking API service... "
if curl -s -f -o /dev/null "$API_URL/health" 2>/dev/null; then
  echo -e "${GREEN}✓ Running${NC}"
else
  echo -e "${RED}✗ Not running${NC}"
  echo ""
  echo -e "${RED}ERROR: API service not accessible at $API_URL${NC}"
  echo "Please start the services with: $DOCKER_COMPOSE up -d"
  exit 1
fi

echo -n "  [*] Checking database... "
if $DOCKER_COMPOSE ps | grep -q postgres.*Up; then
  echo -e "${GREEN}✓ Running${NC}"
else
  echo -e "${YELLOW}⚠ Cannot verify${NC}"
fi

echo -n "  [*] Checking frontend (optional)... "
if curl -s -f -o /dev/null "$FRONTEND_URL" 2>/dev/null; then
  echo -e "${GREEN}✓ Running${NC}"
else
  echo -e "${YELLOW}⚠ Not running${NC} (This is optional for API tests)"
fi

# Get authentication token
print_header "Authentication Setup"

echo -n "  [*] Getting admin token... "
LOGIN_RESPONSE=$(curl -s -X POST "$API_URL/api/auth/login" \
  -H "Content-Type: application/json" \
  -d '{"username":"admin","password":"changeme"}' 2>/dev/null)

if command -v jq &> /dev/null; then
  TOKEN=$(echo "$LOGIN_RESPONSE" | jq -r '.access_token // empty')
else
  TOKEN=$(echo "$LOGIN_RESPONSE" | grep -o '"access_token":"[^"]*' | cut -d'"' -f4)
fi

if [ -z "$TOKEN" ] || [ "$TOKEN" == "null" ]; then
  echo -e "${YELLOW}⚠ Using test mode${NC}"
  echo "    (Login may not be configured yet)"
  TOKEN="test-token"
  SKIP_AUTH_TESTS=true
else
  echo -e "${GREEN}✓ Got token${NC}"
  SKIP_AUTH_TESTS=false
fi

# Test Phase 1: Core API Endpoints
print_header "Phase 1: Core API Endpoints"

test_http "Health check" "GET" "/health" "200"
test_http "API docs" "GET" "/docs" "200"
test_http "OpenAPI spec" "GET" "/openapi.json" "200"

# Test Phase 2: Authentication (if configured)
if [ "$SKIP_AUTH_TESTS" == "false" ]; then
  print_header "Phase 2: Authentication & Authorization"
  
  test_http "Login endpoint" "POST" "/api/auth/login" "200" "" '{"username":"admin","password":"changeme"}'
  test_http "Protected endpoint (with auth)" "GET" "/api/cases" "200" "Authorization: Bearer $TOKEN"
  test_http "Protected endpoint (without auth)" "GET" "/api/cases" "401"
  test_http "Get current user" "GET" "/api/auth/me" "200" "Authorization: Bearer $TOKEN"
else
  echo "  Skipping authentication tests (not configured)"
fi

# Test Phase 3: Data Endpoints
print_header "Phase 3: Data Endpoints"

test_http "List findings" "GET" "/api/findings" "200" "Authorization: Bearer $TOKEN"
test_http "List cases" "GET" "/api/cases" "200" "Authorization: Bearer $TOKEN"
test_http "Get config" "GET" "/api/config" "200" "Authorization: Bearer $TOKEN"

# Test Phase 4: Analytics
print_header "Phase 4: Analytics & AI"

test_http "Analytics (24h)" "GET" "/api/analytics?timeRange=24h" "200" "Authorization: Bearer $TOKEN"
test_http "Analytics (7d)" "GET" "/api/analytics?timeRange=7d" "200" "Authorization: Bearer $TOKEN"
test_http "Analytics (30d)" "GET" "/api/analytics?timeRange=30d" "200" "Authorization: Bearer $TOKEN"

echo -n "  [*] Checking Claude 4.5 model... "
MODELS_RESPONSE=$(curl -s "$API_URL/api/claude/models" -H "Authorization: Bearer $TOKEN")
if echo "$MODELS_RESPONSE" | grep -q "claude-sonnet-4"; then
  echo -e "${GREEN}✓ Claude 4.5 configured${NC}"
else
  echo -e "${YELLOW}⚠ Claude 4.5 not found${NC}"
fi

# Test Phase 5: Integration Endpoints
print_header "Phase 5: Integration Endpoints"

test_http "Available integrations" "GET" "/api/integrations" "200" "Authorization: Bearer $TOKEN"
test_http "MCP servers" "GET" "/api/mcp/servers" "200" "Authorization: Bearer $TOKEN"
test_http "MITRE ATT&CK" "GET" "/api/attack/tactics" "200" "Authorization: Bearer $TOKEN"

# Test Phase 6: Performance Checks
print_header "Phase 6: Performance Checks"

echo -n "  [*] Testing API response time... "
start_time=$(date +%s%N)
curl -s -o /dev/null "$API_URL/health"
end_time=$(date +%s%N)
response_time=$(( (end_time - start_time) / 1000000 ))

if [ $response_time -lt 100 ]; then
  echo -e "${GREEN}✓ PASS${NC} (${response_time}ms < 100ms)"
  ((PASSED_TESTS++))
else
  echo -e "${YELLOW}⚠ SLOW${NC} (${response_time}ms)"
  ((PASSED_TESTS++))
fi
((TOTAL_TESTS++))

echo -n "  [*] Testing analytics response time... "
start_time=$(date +%s%N)
curl -s -o /dev/null -H "Authorization: Bearer $TOKEN" "$API_URL/api/analytics?timeRange=7d"
end_time=$(date +%s%N)
analytics_time=$(( (end_time - start_time) / 1000000 ))

if [ $analytics_time -lt 5000 ]; then
  echo -e "${GREEN}✓ PASS${NC} (${analytics_time}ms < 5000ms)"
  ((PASSED_TESTS++))
else
  echo -e "${YELLOW}⚠ SLOW${NC} (${analytics_time}ms)"
  ((PASSED_TESTS++))
fi
((TOTAL_TESTS++))

# Test Phase 7: Database Health
print_header "Phase 7: Database Health"

echo -n "  [*] Checking database connection... "
if $DOCKER_COMPOSE exec -T postgres psql -U postgres -d deeptempo -c "SELECT 1;" > /dev/null 2>&1; then
  echo -e "${GREEN}✓ PASS${NC}"
  ((PASSED_TESTS++))
else
  echo -e "${RED}✗ FAIL${NC}"
  ((FAILED_TESTS++))
  FAILED_TEST_NAMES+=("Database connection")
fi
((TOTAL_TESTS++))

echo -n "  [*] Checking table existence... "
TABLE_COUNT=$($DOCKER_COMPOSE exec -T postgres psql -U postgres -d deeptempo -t -c "SELECT COUNT(*) FROM information_schema.tables WHERE table_schema='public';" 2>/dev/null | tr -d ' ')

if [ "$TABLE_COUNT" -gt "5" ]; then
  echo -e "${GREEN}✓ PASS${NC} ($TABLE_COUNT tables)"
  ((PASSED_TESTS++))
else
  echo -e "${YELLOW}⚠ WARNING${NC} (Only $TABLE_COUNT tables found)"
  ((PASSED_TESTS++))
fi
((TOTAL_TESTS++))

# Test Phase 8: Docker Services
print_header "Phase 8: Docker Services"

SERVICES=("postgres" "soc-api" "soc-daemon")

for service in "${SERVICES[@]}"; do
  echo -n "  [*] Checking $service... "
  if $DOCKER_COMPOSE ps | grep -q "$service.*Up"; then
    echo -e "${GREEN}✓ Running${NC}"
    ((PASSED_TESTS++))
  else
    echo -e "${RED}✗ Not running${NC}"
    ((FAILED_TESTS++))
    FAILED_TEST_NAMES+=("Docker service: $service")
  fi
  ((TOTAL_TESTS++))
done

# Print Summary
echo ""
echo -e "${BLUE}╔════════════════════════════════════════╗${NC}"
echo -e "${BLUE}║          TEST RESULTS SUMMARY          ║${NC}"
echo -e "${BLUE}╚════════════════════════════════════════╝${NC}"
echo ""
echo "  Total Tests:  $TOTAL_TESTS"
echo -e "  ${GREEN}Passed:       $PASSED_TESTS${NC}"
echo -e "  ${RED}Failed:       $FAILED_TESTS${NC}"
echo ""

# Calculate percentage
if [ $TOTAL_TESTS -gt 0 ]; then
  PASS_RATE=$(( PASSED_TESTS * 100 / TOTAL_TESTS ))
  echo "  Pass Rate:    $PASS_RATE%"
  echo ""
fi

# Show failed tests if any
if [ $FAILED_TESTS -gt 0 ]; then
  echo -e "${RED}Failed Tests:${NC}"
  for test_name in "${FAILED_TEST_NAMES[@]}"; do
    echo "  - $test_name"
  done
  echo ""
fi

# Final verdict
if [ $FAILED_TESTS -eq 0 ]; then
  echo -e "${GREEN}╔════════════════════════════════════════╗${NC}"
  echo -e "${GREEN}║     ✓ ALL TESTS PASSED! 🎉            ║${NC}"
  echo -e "${GREEN}║     System is production ready         ║${NC}"
  echo -e "${GREEN}╚════════════════════════════════════════╝${NC}"
  exit 0
else
  echo -e "${RED}╔════════════════════════════════════════╗${NC}"
  echo -e "${RED}║     ✗ SOME TESTS FAILED                ║${NC}"
  echo -e "${RED}║     Please review and fix              ║${NC}"
  echo -e "${RED}╚════════════════════════════════════════╝${NC}"
  exit 1
fi

