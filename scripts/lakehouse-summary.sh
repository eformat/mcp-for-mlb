#!/usr/bin/env bash
#
# Show a summary of all tables and row counts in the Trino lakehouse.
#
# Usage:
#   ./scripts/lakehouse-summary.sh
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

NAMESPACE="${NAMESPACE:-mlb-agent}"

# Activate venv
VENV="${REPO_DIR}/.venv"
if [ ! -d "$VENV" ]; then
  echo "Creating venv..."
  python3 -m venv "$VENV"
  "$VENV/bin/pip" install -q -r "$REPO_DIR/scripts/requirements.txt"
fi
PYTHON="$VENV/bin/python"

cleanup() {
    kill $TRINO_PF 2>/dev/null || true
}
trap cleanup EXIT

echo "Starting port-forward..."
oc port-forward svc/trino -n "$NAMESPACE" 8090:8080 &>/dev/null &
TRINO_PF=$!
sleep 3

export TRINO_HOST=localhost TRINO_PORT=8090

$PYTHON "${REPO_DIR}/scripts/lakehouse-summary.py"
