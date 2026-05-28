#!/usr/bin/env bash
#
# Deploy the MLB Data Agent from scratch.
#
# Deploys: MinIO → Trino → MCP Server → Agent → SpiceDB
# Prereqs: oc, helm, python3 with trino/pandas
#
# Usage:
#   export MAAS_API_KEY=<jwt_token>
#   ./scripts/deploy-all.sh
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

NAMESPACE="${NAMESPACE:-mlb-agent}"
MAAS_API_KEY="${MAAS_API_KEY:-}"
MAAS_BASE_URL="${MAAS_BASE_URL:-http://maas.apps.ocp.cloud.rhai-tmm.dev/prelude-maas}"

echo "============================================"
echo "  MLB Data Agent — Full Deployment"
echo "============================================"
echo "Namespace: ${NAMESPACE}"
echo ""

# ── 1. Create namespace ───────────────────────────────────────
echo "==> 1. Creating namespace: ${NAMESPACE}"
oc new-project "${NAMESPACE}" 2>/dev/null || oc project "${NAMESPACE}"

# ── 2. Deploy MinIO ───────────────────────────────────────────
echo "==> 2. Deploying MinIO"
oc apply -k "${REPO_DIR}/deploy/minio/overlays/cluster-dev" -n "${NAMESPACE}"
echo "Waiting for MinIO..."
oc rollout status deployment/minio -n "${NAMESPACE}" --timeout=120s

# ── 3. Deploy Trino ───────────────────────────────────────────
echo "==> 3. Deploying Trino"
if [ -z "${MAAS_API_KEY}" ]; then
  echo "WARNING: MAAS_API_KEY not set. LLM catalog in Trino will not work."
fi

# Create S3 credentials secret for Trino
oc create secret generic s3-credentials \
  --from-literal=S3_ACCESS_KEY=minio \
  --from-literal=S3_SECRET_KEY=minio1234 \
  -n "${NAMESPACE}" 2>/dev/null || echo "S3 secret already exists"

# Create LLM API key secret for Trino
oc create secret generic trino-llm-api-key \
  --from-literal=OPENAI_API_KEY="${MAAS_API_KEY:-not-set}" \
  -n "${NAMESPACE}" 2>/dev/null || echo "LLM secret already exists"

# Deploy Nessie
echo "Deploying Nessie..."
oc apply -f "${REPO_DIR}/deploy/trino-chart/nessie/deployment.yaml" -n "${NAMESPACE}"
oc apply -f "${REPO_DIR}/deploy/trino-chart/nessie/service.yaml" -n "${NAMESPACE}"
oc rollout status deployment/nessie -n "${NAMESPACE}" --timeout=120s

# Deploy Trino via Helm
echo "Deploying Trino via Helm..."
helm upgrade --install trino "${REPO_DIR}/deploy/trino-chart/trino" \
  -n "${NAMESPACE}" \
  --wait --timeout 120s

# ── 4. Load data into Trino ──────────────────────────────────
echo "==> 4. Loading data into Trino"
oc port-forward svc/trino -n "${NAMESPACE}" 8090:8080 &
PF_PID=$!
sleep 5

echo "Loading baseball data..."
TRINO_HOST=localhost TRINO_PORT=8090 \
  DATA_DIR="${REPO_DIR}/data/baseball" \
  python3 "${REPO_DIR}/scripts/load-baseball-trino.py"

echo "Loading weather data..."
TRINO_HOST=localhost TRINO_PORT=8090 \
  DATA_DIR="${REPO_DIR}/data/weather" \
  python3 "${REPO_DIR}/scripts/load-weather-trino.py"

echo "Loading pitch data..."
TRINO_HOST=localhost TRINO_PORT=8090 \
  MINIO_ENDPOINT=localhost:9001 \
  DATA_DIR="${REPO_DIR}/data/pitch" \
  python3 "${REPO_DIR}/scripts/load-pitch-trino.py"

echo "Loading live 2026 season data..."
TRINO_HOST=localhost TRINO_PORT=8090 \
  MINIO_ENDPOINT=localhost:9001 \
  CACHE_DIR="${REPO_DIR}/data/live" \
  python3 "${REPO_DIR}/scripts/load-live-trino.py"

kill $PF_PID 2>/dev/null || true

# ── 5. Create secrets ─────────────────────────────────────────
echo "==> 5. Creating secrets"
oc create secret generic mlb-agent-maas-key \
  --from-literal=api-key="${MAAS_API_KEY:-not-set}" \
  -n "${NAMESPACE}" 2>/dev/null || echo "MAAS key secret already exists"

# ── 6. Deploy SpiceDB ────────────────────────────────────────
echo "==> 6. Deploying SpiceDB"
oc apply -f "${REPO_DIR}/deploy/spicedb/" -n "${NAMESPACE}" 2>/dev/null || echo "SpiceDB resources applied"

# Seed SpiceDB (requires port-forward)
echo "Seeding SpiceDB (if available)..."
oc port-forward svc/dev -n "${NAMESPACE}" 50051:50051 &
SPICEDB_PF_PID=$!
sleep 3
SPICEDB_ENDPOINT=localhost:50051 \
  python3 "${REPO_DIR}/agents/mlb-agent/spicedb/seed_relationships.py" 2>/dev/null || echo "SpiceDB seed skipped"
kill $SPICEDB_PF_PID 2>/dev/null || true

# ── 7. Deploy RBAC ────────────────────────────────────────────
echo "==> 7. Deploying RBAC"
oc apply -f "${REPO_DIR}/deploy/mlflow-rbac.yaml" -n "${NAMESPACE}"
oc apply -f "${REPO_DIR}/deploy/pipeline-mlflow-rbac.yaml" -n "${NAMESPACE}" 2>/dev/null || true

# ── 8. Deploy MCP server ─────────────────────────────────────
echo "==> 8. Deploying MCP server"
oc apply -k "${REPO_DIR}/deploy/mlb-mcp-server" -n "${NAMESPACE}"
oc rollout status deployment/mlb-mcp-server -n "${NAMESPACE}" --timeout=120s

# ── 9. Deploy agent ───────────────────────────────────────────
echo "==> 9. Deploying agent"
oc apply -k "${REPO_DIR}/agents/mlb-agent/deploy" -n "${NAMESPACE}"
oc rollout status deployment/mlb-agent -n "${NAMESPACE}" --timeout=120s

# ── 10. Deploy pipeline server (DSPA) ─────────────────────────
echo "==> 10. Deploying pipeline server"
oc apply -f "${REPO_DIR}/deploy/pipeline-s3-secret.yaml" -n "${NAMESPACE}"
oc apply -f "${REPO_DIR}/deploy/dspa.yaml" -n "${NAMESPACE}"
echo "Waiting for DSPA..."
sleep 30
oc rollout status deployment/ds-pipeline-dspa -n "${NAMESPACE}" --timeout=120s
"${REPO_DIR}/scripts/fix-dspa-charset.sh"

# ── 11. Summary ───────────────────────────────────────────────
echo ""
echo "============================================"
echo "  Deployment Complete"
echo "============================================"
echo ""
echo "Routes:"
oc get routes -n "${NAMESPACE}" -o custom-columns='NAME:.metadata.name,HOST:.spec.host' --no-headers 2>/dev/null || echo "  (no routes found)"
echo ""
echo "Pods:"
oc get pods -n "${NAMESPACE}" --no-headers | awk '{print "  " $1, $2, $3}'
echo ""
echo "Login: admin / admin"
echo ""
echo "Sample questions to try:"
echo "  - Who hit the most home runs in a single season?"
echo "  - List all World Series winners since 2000"
echo "  - Compare career ERAs of Pedro Martinez and Greg Maddux"
echo "  - How does temperature affect home run rates?"
