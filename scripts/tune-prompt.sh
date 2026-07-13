#!/usr/bin/env bash
# Run RL prompt tuning loop with port-forwards to Trino and MLflow.
# Usage: ./scripts/tune-prompt.sh [--dry-run] [--max-steps N] [--batch-size N]
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
NAMESPACE="${NAMESPACE:-mlb-agent}"
VENV="${REPO_DIR}/.venv"
PYTHON="${VENV}/bin/python"

cleanup() {
    kill $TRINO_PF 2>/dev/null || true
    kill $MLFLOW_PF 2>/dev/null || true
}
trap cleanup EXIT

# Kill stale port-forwards
kill $(lsof -ti:8090) 2>/dev/null || true
kill $(lsof -ti:8443) 2>/dev/null || true
sleep 1

echo "==> Starting port-forwards..."
oc port-forward svc/trino -n "$NAMESPACE" 8090:8080 &>/dev/null &
TRINO_PF=$!
oc port-forward svc/mlflow -n redhat-ods-applications 8443:8443 &>/dev/null &
MLFLOW_PF=$!
sleep 3

SA_TOKEN=$(oc whoami -t)

export TRINO_QUERY_HOST=localhost
export TRINO_QUERY_PORT=8090
export MLFLOW_TRACKING_URI=https://localhost:8443/mlflow
export MLFLOW_TRACKING_INSECURE_TLS=true
export MLFLOW_TRACKING_TOKEN="$SA_TOKEN"
export MLFLOW_WORKSPACE="$NAMESPACE"
export MODEL_NAME="${MODEL_NAME:-qwen36-27b}"
export MODEL_ENDPOINT="${MODEL_ENDPOINT:-https://maas.apps.ocp.cloud.rhai-tmm.dev/prelude-maas/${MODEL_NAME}/v1}"
export OPENAI_API_KEY="${OPENAI_API_KEY:-$(oc get secret mlb-agent-maas-key -n "$NAMESPACE" -o jsonpath='{.data.api-key}' | base64 -d)}"

# Check prediction_history table exists
echo "==> Checking prediction_history table..."
$PYTHON -c "
from trino.dbapi import connect
conn = connect(host='localhost', port=8090, user='admin', catalog='lakehouse', schema='mlb')
cur = conn.cursor()
try:
    cur.execute('SELECT COUNT(*) FROM lakehouse.mlb.prediction_history WHERE was_correct IS NOT NULL')
    count = cur.fetchone()[0]
    print(f'Found {count} resolved predictions.')
    if count == 0:
        print('ERROR: No resolved predictions. Load data first: make load-predictions')
        exit(1)
except Exception as e:
    if 'TABLE_NOT_FOUND' in str(e):
        print('ERROR: prediction_history table missing. Load it first: make load-predictions')
        exit(1)
    raise
finally:
    conn.close()
"

echo "==> Running prompt tuning (model=${MODEL_NAME})..."
$PYTHON "${REPO_DIR}/prompt_tuning/run_tuning.py" "$@"
