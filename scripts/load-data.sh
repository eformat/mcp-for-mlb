#!/usr/bin/env bash
#
# Load all data into Trino via port-forward.
# Handles port-forward lifecycle automatically.
#
# Usage:
#   ./scripts/load-data.sh              # Load all datasets
#   ./scripts/load-data.sh baseball     # Load only baseball
#   ./scripts/load-data.sh weather      # Load only weather
#   ./scripts/load-data.sh pitch        # Load only pitch
#   ./scripts/load-data.sh live         # Load only live 2026 season
#   ./scripts/load-data.sh upload       # Upload raw CSVs to MinIO
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

NAMESPACE="${NAMESPACE:-mlb-agent}"
DATASET="${1:-all}"

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
    kill $MINIO_PF 2>/dev/null || true
}
trap cleanup EXIT

echo "Starting port-forwards..."
oc port-forward svc/trino -n "$NAMESPACE" 8090:8080 &>/dev/null &
TRINO_PF=$!
oc port-forward svc/minio -n "$NAMESPACE" 9000:9000 &>/dev/null &
MINIO_PF=$!
sleep 3

export TRINO_HOST=localhost TRINO_PORT=8090 MINIO_ENDPOINT=localhost:9000

case "$DATASET" in
    baseball)
        DATA_DIR="${REPO_DIR}/data/baseball" $PYTHON "${REPO_DIR}/scripts/load-baseball-trino.py"
        ;;
    weather)
        DATA_DIR="${REPO_DIR}/data/weather" $PYTHON "${REPO_DIR}/scripts/load-weather-trino.py"
        ;;
    pitch)
        DATA_DIR="${REPO_DIR}/data/pitch" $PYTHON "${REPO_DIR}/scripts/load-pitch-trino.py"
        ;;
    live)
        CACHE_DIR="${REPO_DIR}/data/live" $PYTHON "${REPO_DIR}/scripts/load-live-trino.py"
        ;;
    upload)
        $PYTHON "${REPO_DIR}/scripts/upload-data-minio.py"
        ;;
    all)
        DATA_DIR="${REPO_DIR}/data/baseball" $PYTHON "${REPO_DIR}/scripts/load-baseball-trino.py"
        DATA_DIR="${REPO_DIR}/data/weather" $PYTHON "${REPO_DIR}/scripts/load-weather-trino.py"
        DATA_DIR="${REPO_DIR}/data/pitch" $PYTHON "${REPO_DIR}/scripts/load-pitch-trino.py"
        CACHE_DIR="${REPO_DIR}/data/live" $PYTHON "${REPO_DIR}/scripts/load-live-trino.py"
        $PYTHON "${REPO_DIR}/scripts/upload-data-minio.py"
        ;;
    *)
        echo "Unknown dataset: $DATASET"
        echo "Usage: $0 [all|baseball|weather|pitch|live|upload]"
        exit 1
        ;;
esac

echo "Done."
