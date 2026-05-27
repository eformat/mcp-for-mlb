#!/usr/bin/env bash
# ============================================================================
# TRINO AI FUNCTIONS - EXAMPLE RUNNER
# ============================================================================
# Runs all SQL examples against a Trino server and reports results.
#
# Usage:
#   ./examples/run_all.sh                                    # defaults to localhost:8080
#   TRINO_SERVER=http://somehost:8080 ./examples/run_all.sh  # custom server
# ============================================================================

set -uo pipefail

SERVER="${TRINO_SERVER:-http://localhost:8080}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
BOLD='\033[1m'
NC='\033[0m'

log_info()    { echo -e "${BLUE}→${NC} $1"; }
log_success() { echo -e "${GREEN}✓${NC} $1"; }
log_warn()    { echo -e "${YELLOW}⚠${NC} $1"; }
log_error()   { echo -e "${RED}✗${NC} $1"; }

PASS=0
FAIL=0
TOTAL=0
START_SECONDS=$SECONDS

echo ""
echo "============================================"
echo "  Trino AI Functions - Example Runner"
echo "============================================"
echo ""
log_info "Server: ${BOLD}${SERVER}${NC}"
echo ""

for f in "${SCRIPT_DIR}"/*.sql; do
    TOTAL=$((TOTAL + 1))
    name="$(basename "$f")"
    comment=$(head -1 "$f" | sed 's/^-- *//')

    echo -e "${BOLD}[${TOTAL}]${NC} ${name}"
    log_info "${comment}"

    output=$(trino --server "$SERVER" -f "$f" 2>&1)
    rc=$?

    if [ $rc -eq 0 ]; then
        rows=$(echo "$output" | wc -l)
        log_success "PASS (${rows} rows)"
        PASS=$((PASS + 1))
    else
        log_error "FAIL"
        echo "$output" | head -3 | sed 's/^/    /'
        FAIL=$((FAIL + 1))
    fi
    echo ""
done

ELAPSED=$(( SECONDS - START_SECONDS ))

echo "============================================"
echo -e "  Results: ${GREEN}${PASS} passed${NC}, ${RED}${FAIL} failed${NC}, ${TOTAL} total"
echo "  Elapsed: ${ELAPSED}s"
echo "============================================"
echo ""

[ $FAIL -eq 0 ] && exit 0 || exit 1
