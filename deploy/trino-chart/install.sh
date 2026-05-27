#!/bin/bash
# ============================================================================
# TRINO AI + S3 LAKEHOUSE — ALL-IN-ONE INSTALLER
# ============================================================================
# Deploys Trino with AI functions and an Iceberg lakehouse on MinIO S3
# to an OpenShift cluster. Optionally loads HuggingFace datasets.
#
# Usage:
#   export OPENAI_API_KEY=<key>
#   export OPENAI_BASE_URL=https://your-llm-endpoint/v1
#   ./install.sh
#
# Prerequisites:
#   - oc CLI authenticated to an OpenShift cluster
#   - helm >= 3
# ============================================================================

set -o pipefail

readonly RED='\033[0;31m'
readonly GREEN='\033[0;32m'
readonly ORANGE='\033[38;5;214m'
readonly NC='\033[0m'
readonly SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# ---------------------------------------------------------------------------
# Environment variables
# ---------------------------------------------------------------------------
OPENAI_API_KEY=${OPENAI_API_KEY:-}
OPENAI_BASE_URL=${OPENAI_BASE_URL:-}
OPENAI_MODEL=${OPENAI_MODEL:-llama-4-scout-17b-16e-w4a16}
S3_ACCESS_KEY=${S3_ACCESS_KEY:-minio}
S3_SECRET_KEY=${S3_SECRET_KEY:-minio1234}
S3_BUCKET=${S3_BUCKET:-warehouse}
MINIO_PVC_SIZE=${MINIO_PVC_SIZE:-100Gi}
HF_TOKEN=${HF_TOKEN:-}
TRINO_NAMESPACE=${TRINO_NAMESPACE:-trino}
MINIO_NAMESPACE=${MINIO_NAMESPACE:-minio}
POSTGRES_HOST=${POSTGRES_HOST:-}
POSTGRES_PORT=${POSTGRES_PORT:-5432}
POSTGRES_DB=${POSTGRES_DB:-vectordb}
POSTGRES_USER=${POSTGRES_USER:-postgres}
POSTGRES_PASSWORD=${POSTGRES_PASSWORD:-password}
SKIP_MINIO=${SKIP_MINIO:-}
SKIP_UI=${SKIP_UI:-}
SKIP_DATA=${SKIP_DATA:-}

# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------
[ -z "$OPENAI_API_KEY" ] && echo "🕱 Error: OPENAI_API_KEY not set in env" && exit 1
[ -z "$OPENAI_BASE_URL" ] && echo "🕱 Error: OPENAI_BASE_URL not set in env" && exit 1
[ -z "$SKIP_DATA" ] && [ -z "$HF_TOKEN" ] && echo "🕱 Error: HF_TOKEN not set in env (set SKIP_DATA=true to skip dataset loading)" && exit 1

if ! command -v oc &>/dev/null; then
    echo "🕱 Error: oc CLI not found in PATH"
    exit 1
fi

if ! command -v helm &>/dev/null; then
    echo "🕱 Error: helm not found in PATH"
    exit 1
fi

if ! oc whoami &>/dev/null; then
    echo "🕱 Error: not logged in to OpenShift cluster"
    exit 1
fi

# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------
wait_for_pod() {
    local ns="$1"
    local label="$2"
    local i=0
    echo "🌴 Running wait_for_pod $label in $ns..."
    until [ "$(oc -n "$ns" get pods -l "$label" -o jsonpath='{.items[0].status.conditions[?(@.type=="Ready")].status}' 2>/dev/null)" == "True" ]
    do
        echo -e "${GREEN}Waiting for pod $label in $ns...${NC}"
        sleep 5
        ((i=i+1))
        if [ $i -gt 60 ]; then
            echo -e "🕱${RED}Failed - pod $label in $ns never ready?${NC}"
            exit 1
        fi
    done
    echo "🌴 wait_for_pod $label in $ns ran OK"
}

wait_for_job() {
    local ns="$1"
    local name="$2"
    local i=0
    echo "🌴 Running wait_for_job $name in $ns..."
    until [ "$(oc -n "$ns" get job "$name" -o jsonpath='{.status.succeeded}' 2>/dev/null)" == "1" ]
    do
        echo -e "${GREEN}Waiting for job $name in $ns...${NC}"
        sleep 10
        ((i=i+1))
        if [ $i -gt 120 ]; then
            echo -e "🕱${RED}Failed - job $name in $ns never completed?${NC}"
            oc -n "$ns" logs "job/$name" --tail=20 2>/dev/null
            exit 1
        fi
        # Check for failure
        local failed
        failed=$(oc -n "$ns" get job "$name" -o jsonpath='{.status.failed}' 2>/dev/null)
        if [ "$failed" -gt 0 ] 2>/dev/null; then
            echo -e "🕱${RED}Failed - job $name failed${NC}"
            oc -n "$ns" logs "job/$name" --tail=30 2>/dev/null
            exit 1
        fi
    done
    echo "🌴 wait_for_job $name in $ns ran OK"
}

# ---------------------------------------------------------------------------
# check_done — early exit if already installed
# ---------------------------------------------------------------------------
check_done() {
    echo "🌴 Running check_done..."
    local ready
    ready=$(oc -n "$TRINO_NAMESPACE" get pods -l app.kubernetes.io/component=coordinator -o jsonpath='{.items[0].status.conditions[?(@.type=="Ready")].status}' 2>/dev/null)
    if [ "$ready" == "True" ]; then
        echo "🌴 Trino coordinator already running"
        return 0
    fi
    echo -e "💀${ORANGE}Warn - Trino not ready yet, continuing with install${NC}"
    return 1
}

# ---------------------------------------------------------------------------
# deploy_minio
# ---------------------------------------------------------------------------
deploy_minio() {
    if [ ! -z "$SKIP_MINIO" ]; then
        echo -e "${GREEN}Ignoring - deploy_minio - SKIP_MINIO set${NC}"
        return
    fi
    echo "🌴 Running deploy_minio..."
    oc create namespace "$MINIO_NAMESPACE" 2>/dev/null || true
    for f in "${SCRIPT_DIR}"/minio/base/minio-*.yaml; do
        if [ "$(basename "$f")" == "minio-pvc.yaml" ]; then
            sed "s/storage: 100Gi/storage: ${MINIO_PVC_SIZE}/" "$f" | oc apply -n "$MINIO_NAMESPACE" -f-
        else
            oc apply -f "$f" -n "$MINIO_NAMESPACE"
        fi
    done
    if [ "$?" != 0 ]; then
        echo -e "🕱${RED}Failed - deploy_minio?${NC}"
        exit 1
    fi
    wait_for_pod "$MINIO_NAMESPACE" "app.kubernetes.io/name=minio"
    echo "🌴 deploy_minio ran OK"
}

# ---------------------------------------------------------------------------
# create_minio_bucket — K8s Job using minio/mc
# ---------------------------------------------------------------------------
create_minio_bucket() {
    if [ ! -z "$SKIP_MINIO" ]; then
        echo -e "${GREEN}Ignoring - create_minio_bucket - SKIP_MINIO set${NC}"
        return
    fi
    echo "🌴 Running create_minio_bucket..."
    local bucket_done
    bucket_done=$(oc -n "$MINIO_NAMESPACE" get job minio-create-bucket -o jsonpath='{.status.succeeded}' 2>/dev/null)
    if [ "$bucket_done" == "1" ]; then
        echo "🌴 Bucket job already completed, skipping"
        return
    fi
    oc -n "$MINIO_NAMESPACE" delete job minio-create-bucket 2>/dev/null || true
    cat <<EOF | oc apply -f-
apiVersion: batch/v1
kind: Job
metadata:
  name: minio-create-bucket
  namespace: ${MINIO_NAMESPACE}
spec:
  backoffLimit: 3
  template:
    spec:
      restartPolicy: Never
      securityContext:
        runAsNonRoot: true
      containers:
        - name: mc
          image: docker.io/minio/mc:latest
          command: ["sh", "-c"]
          args:
            - |
              export HOME=/tmp &&
              mc alias set myminio http://minio.${MINIO_NAMESPACE}.svc.cluster.local:9000 ${S3_ACCESS_KEY} ${S3_SECRET_KEY} &&
              mc mb --ignore-existing myminio/${S3_BUCKET} &&
              echo "Bucket ${S3_BUCKET} ready"
          securityContext:
            allowPrivilegeEscalation: false
            capabilities:
              drop:
                - ALL
EOF
    if [ "$?" != 0 ]; then
        echo -e "🕱${RED}Failed - create_minio_bucket job?${NC}"
        exit 1
    fi
    wait_for_job "$MINIO_NAMESPACE" "minio-create-bucket"
    echo "🌴 create_minio_bucket ran OK"
}

# ---------------------------------------------------------------------------
# deploy_nessie — Iceberg catalog server
# ---------------------------------------------------------------------------
deploy_nessie() {
    echo "🌴 Running deploy_nessie..."
    oc create namespace "$TRINO_NAMESPACE" 2>/dev/null || true
    oc apply -f "${SCRIPT_DIR}/nessie/" -n "$TRINO_NAMESPACE"
    if [ "$?" != 0 ]; then
        echo -e "🕱${RED}Failed - deploy_nessie?${NC}"
        exit 1
    fi
    wait_for_pod "$TRINO_NAMESPACE" "app=nessie"
    echo "🌴 deploy_nessie ran OK"
}

# ---------------------------------------------------------------------------
# create_secrets — Trino secret with LLM + S3 credentials
# ---------------------------------------------------------------------------
create_secrets() {
    echo "🌴 Running create_secrets..."
    oc -n "$TRINO_NAMESPACE" delete secret trino-llm-api-key 2>/dev/null || true
    local secret_args=(
        --from-literal=OPENAI_API_KEY="${OPENAI_API_KEY}"
        --from-literal=S3_ACCESS_KEY="${S3_ACCESS_KEY}"
        --from-literal=S3_SECRET_KEY="${S3_SECRET_KEY}"
    )
    [ ! -z "$POSTGRES_HOST" ] && secret_args+=(--from-literal=POSTGRES_PASSWORD="${POSTGRES_PASSWORD}")
    oc -n "$TRINO_NAMESPACE" create secret generic trino-llm-api-key "${secret_args[@]}"
    if [ "$?" != 0 ]; then
        echo -e "🕱${RED}Failed - create_secrets?${NC}"
        exit 1
    fi
    echo "🌴 create_secrets ran OK"
}

# ---------------------------------------------------------------------------
# deploy_trino — Helm install/upgrade
# ---------------------------------------------------------------------------
deploy_trino() {
    echo "🌴 Running deploy_trino..."
    cat <<EOF > /tmp/trino-install-values.yaml
catalogs:
  llm: |
    connector.name=ai
    ai.provider=openai
    ai.model=${OPENAI_MODEL}
    ai.openai.endpoint=${OPENAI_BASE_URL}
    ai.openai.api-key=\${ENV:OPENAI_API_KEY}
  lakehouse: |
    connector.name=iceberg
    iceberg.catalog.type=nessie
    iceberg.nessie-catalog.uri=http://nessie.${TRINO_NAMESPACE}.svc.cluster.local:19120/api/v2
    iceberg.nessie-catalog.default-warehouse-dir=s3://${S3_BUCKET}/
    fs.native-s3.enabled=true
    s3.endpoint=http://minio.${MINIO_NAMESPACE}.svc.cluster.local:9000
    s3.region=us-east-1
    s3.path-style-access=true
    s3.aws-access-key=\${ENV:S3_ACCESS_KEY}
    s3.aws-secret-key=\${ENV:S3_SECRET_KEY}
EOF
    if [ ! -z "$POSTGRES_HOST" ]; then
        echo "🌴 Adding PostgreSQL catalog (vectordb)..."
        cat <<EOF >> /tmp/trino-install-values.yaml
  vectordb: |
    connector.name=postgresql
    connection-url=jdbc:postgresql://${POSTGRES_HOST}:${POSTGRES_PORT}/${POSTGRES_DB}
    connection-user=${POSTGRES_USER}
    connection-password=\${ENV:POSTGRES_PASSWORD}
EOF
    fi
    helm upgrade --install trino "${SCRIPT_DIR}/trino" \
        -n "$TRINO_NAMESPACE" \
        -f /tmp/trino-install-values.yaml
    local rc=$?
    rm -f /tmp/trino-install-values.yaml
    if [ "$rc" != 0 ]; then
        echo -e "🕱${RED}Failed - deploy_trino helm?${NC}"
        exit 1
    fi
    wait_for_pod "$TRINO_NAMESPACE" "app.kubernetes.io/component=coordinator"
    echo "🌴 deploy_trino ran OK"
}

# ---------------------------------------------------------------------------
# deploy_query_ui
# ---------------------------------------------------------------------------
deploy_query_ui() {
    if [ ! -z "$SKIP_UI" ]; then
        echo -e "${GREEN}Ignoring - deploy_query_ui - SKIP_UI set${NC}"
        return
    fi
    echo "🌴 Running deploy_query_ui..."
    local chart_dir="${SCRIPT_DIR}/../trino-query-ui/chart"
    if [ ! -d "$chart_dir" ]; then
        echo -e "💀${ORANGE}Warn - trino-query-ui chart not found at $chart_dir, skipping${NC}"
        return
    fi
    helm upgrade --install trino-query-ui "$chart_dir" -n "$TRINO_NAMESPACE" \
        --set "trinoUpstream=trino.${TRINO_NAMESPACE}.svc.cluster.local:8080"
    if [ "$?" != 0 ]; then
        echo -e "🕱${RED}Failed - deploy_query_ui helm?${NC}"
        exit 1
    fi
    wait_for_pod "$TRINO_NAMESPACE" "app=trino-query-ui"
    echo "🌴 deploy_query_ui ran OK"
}

# ---------------------------------------------------------------------------
# load_datasets — K8s Job to download HF data and load into Trino
# ---------------------------------------------------------------------------
load_datasets() {
    if [ ! -z "$SKIP_DATA" ]; then
        echo -e "${GREEN}Ignoring - load_datasets - SKIP_DATA set${NC}"
        return
    fi
    echo "🌴 Running load_datasets..."
    local data_done
    data_done=$(oc -n "$TRINO_NAMESPACE" get job trino-load-data -o jsonpath='{.status.succeeded}' 2>/dev/null)
    if [ "$data_done" == "1" ]; then
        echo "🌴 Data loading job already completed, skipping"
        return
    fi

    oc -n "$TRINO_NAMESPACE" delete configmap trino-data-loader 2>/dev/null || true
    cat <<'PYEOF' > /tmp/trino-loader.py
#!/usr/bin/env python3
"""Load HuggingFace datasets into Trino Iceberg tables."""
import csv
import os
import subprocess
import sys

def run(cmd):
    print(f"$ {cmd}")
    subprocess.check_call(cmd, shell=True)

def load_csv(cur, catalog, schema, table, csv_path, columns):
    print(f"Creating schema {catalog}.{schema}...")
    cur.execute(f"CREATE SCHEMA IF NOT EXISTS {catalog}.{schema}")
    print(f"Creating table {table}...")
    cur.execute(f"DROP TABLE IF EXISTS {catalog}.{schema}.{table}")
    cols = ", ".join(f"{c} VARCHAR" for c in columns)
    cur.execute(f"CREATE TABLE {catalog}.{schema}.{table} ({cols})")

    print(f"Loading data from {csv_path}...")
    with open(csv_path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        batch, total = [], 0
        for row in reader:
            vals = ", ".join(f"'{str(row[c]).replace(chr(39), chr(39)+chr(39))}'" for c in columns)
            batch.append(f"({vals})")
            if len(batch) >= 500:
                cur.execute(f"INSERT INTO {catalog}.{schema}.{table} VALUES " + ", ".join(batch))
                total += len(batch)
                batch = []
                if total % 5000 == 0:
                    print(f"  Inserted {total} rows...")
        if batch:
            cur.execute(f"INSERT INTO {catalog}.{schema}.{table} VALUES " + ", ".join(batch))
            total += len(batch)
    print(f"Done. {total} rows loaded into {catalog}.{schema}.{table}")

def load_parquet(cur, catalog, schema, table, parquet_path, columns, rename=None):
    import pandas as pd
    print(f"Creating schema {catalog}.{schema}...")
    cur.execute(f"CREATE SCHEMA IF NOT EXISTS {catalog}.{schema}")
    print(f"Creating table {table}...")
    cur.execute(f"DROP TABLE IF EXISTS {catalog}.{schema}.{table}")
    cols = ", ".join(f"{c} VARCHAR" for c in columns)
    cur.execute(f"CREATE TABLE {catalog}.{schema}.{table} ({cols})")

    print(f"Loading data from {parquet_path}...")
    df = pd.read_parquet(parquet_path)
    if rename:
        df = df.rename(columns=rename)

    total = 0
    for start in range(0, len(df), 500):
        batch = df.iloc[start:start + 500]
        values = ", ".join(
            "(" + ", ".join(f"'{str(val).replace(chr(39), chr(39)+chr(39))}'" for val in row) + ")"
            for _, row in batch.iterrows()
        )
        cur.execute(f"INSERT INTO {catalog}.{schema}.{table} VALUES {values}")
        total += len(batch)
        if total % 5000 == 0 or total == len(df):
            print(f"  Inserted {total}/{len(df)} rows...")
    print(f"Done. {total} rows loaded into {catalog}.{schema}.{table}")

# Main
TRINO_HOST = os.environ.get("TRINO_HOST", "trino." + os.environ.get("TRINO_NAMESPACE", "trino") + ".svc.cluster.local")
HF_TOKEN = os.environ.get("HF_TOKEN", "")

print("=== Downloading datasets ===")
os.environ["PATH"] = "/tmp/.local/bin:" + os.environ.get("PATH", "")
run(f"pip install --break-system-packages --user -q huggingface_hub trino pandas pyarrow")
import site; site.addsitedir(site.getusersitepackages())

from huggingface_hub import snapshot_download
print("Downloading hotel reviews...")
snapshot_download("Aditya1010/17k-hotel-reviews-dataset", repo_type="dataset", local_dir="/tmp/hotel-reviews", token=HF_TOKEN)
print("Downloading financial sentiment...")
snapshot_download("NOSIBLE/financial-sentiment", repo_type="dataset", local_dir="/tmp/financial-sentiment", token=HF_TOKEN)

print("=== Connecting to Trino ===")
from trino.dbapi import connect
conn = connect(host=TRINO_HOST, port=8080, user="admin")
cur = conn.cursor()

print("\n=== Loading hotel reviews ===")
load_csv(cur, "lakehouse", "reviews", "hotel_reviews", "/tmp/hotel-reviews/balanced_dataset.csv", ["review", "label"])

print("\n=== Loading financial sentiment ===")
load_parquet(cur, "lakehouse", "finance", "financial_news", "/tmp/financial-sentiment/data.parquet",
             ["text", "label", "source", "url"], rename={"netloc": "source"})

cur.close()
conn.close()
print("\n=== All datasets loaded successfully ===")
PYEOF

    oc -n "$TRINO_NAMESPACE" create configmap trino-data-loader --from-file=loader.py=/tmp/trino-loader.py
    rm -f /tmp/trino-loader.py

    oc -n "$TRINO_NAMESPACE" delete job trino-load-data 2>/dev/null || true
    cat <<EOF | oc apply -f-
apiVersion: batch/v1
kind: Job
metadata:
  name: trino-load-data
  namespace: ${TRINO_NAMESPACE}
spec:
  backoffLimit: 2
  activeDeadlineSeconds: 1800
  template:
    spec:
      restartPolicy: Never
      securityContext:
        runAsNonRoot: true
      containers:
        - name: loader
          image: python:3.12-slim
          command: ["python", "/scripts/loader.py"]
          env:
            - name: HOME
              value: /tmp
            - name: HF_TOKEN
              value: "${HF_TOKEN}"
            - name: TRINO_NAMESPACE
              value: "${TRINO_NAMESPACE}"
          volumeMounts:
            - name: scripts
              mountPath: /scripts
          securityContext:
            allowPrivilegeEscalation: false
            capabilities:
              drop:
                - ALL
      volumes:
        - name: scripts
          configMap:
            name: trino-data-loader
EOF
    if [ "$?" != 0 ]; then
        echo -e "🕱${RED}Failed - load_datasets job?${NC}"
        exit 1
    fi
    echo "🌴 Data loading job submitted - this may take 10-15 minutes"
    echo "🌴 Monitor with: oc -n $TRINO_NAMESPACE logs -f job/trino-load-data"
    wait_for_job "$TRINO_NAMESPACE" "trino-load-data"
    echo "🌴 load_datasets ran OK"
}

# ---------------------------------------------------------------------------
# all — orchestrator
# ---------------------------------------------------------------------------
all() {
    echo ""
    echo "============================================"
    echo "  Trino AI + S3 Lakehouse Installer"
    echo "============================================"
    echo ""
    echo "🌴 OPENAI_MODEL set to $OPENAI_MODEL"
    echo "🌴 OPENAI_BASE_URL set to $OPENAI_BASE_URL"
    echo "🌴 TRINO_NAMESPACE set to $TRINO_NAMESPACE"
    echo "🌴 MINIO_NAMESPACE set to $MINIO_NAMESPACE"
    echo "🌴 S3_BUCKET set to $S3_BUCKET"
    echo "🌴 MINIO_PVC_SIZE set to $MINIO_PVC_SIZE"
    [ ! -z "$POSTGRES_HOST" ] && echo "🌴 POSTGRES_HOST set to $POSTGRES_HOST"
    [ ! -z "$SKIP_MINIO" ] && echo "🌴 SKIP_MINIO set"
    [ ! -z "$SKIP_UI" ] && echo "🌴 SKIP_UI set"
    [ ! -z "$SKIP_DATA" ] && echo "🌴 SKIP_DATA set"
    echo ""

    if check_done; then
        echo -e "💀${ORANGE}Warn - Trino already running. Re-run with changes will upgrade.${NC}"
    fi

    deploy_minio
    create_minio_bucket
    deploy_nessie
    create_secrets
    deploy_trino
    deploy_query_ui
    load_datasets

    echo ""
    echo "============================================"
    echo "  Routes"
    echo "============================================"
    oc get route -n "$TRINO_NAMESPACE" --no-headers 2>/dev/null | awk '{printf "  %-20s https://%s\n", $1, $2}'
    echo ""
}

# ---------------------------------------------------------------------------
# usage
# ---------------------------------------------------------------------------
usage() {
    cat <<EOF 2>&1
Usage: $0

Required environment variables:
  OPENAI_API_KEY       LLM API key
  OPENAI_BASE_URL      OpenAI-compatible endpoint URL
  HF_TOKEN             HuggingFace token (or set SKIP_DATA=true)

Optional:
  OPENAI_MODEL         Model name (default: llama-4-scout-17b-16e-w4a16)
  S3_ACCESS_KEY        MinIO access key (default: minio)
  S3_SECRET_KEY        MinIO secret key (default: minio1234)
  S3_BUCKET            S3 bucket name (default: warehouse)
  MINIO_PVC_SIZE       MinIO PVC size (default: 100Gi)
  TRINO_NAMESPACE      Trino namespace (default: trino)
  MINIO_NAMESPACE      MinIO namespace (default: minio)
  POSTGRES_HOST        PostgreSQL host (optional, enables vectordb catalog)
  POSTGRES_PORT        PostgreSQL port (default: 5432)
  POSTGRES_DB          PostgreSQL database (default: vectordb)
  POSTGRES_USER        PostgreSQL user (default: postgres)
  POSTGRES_PASSWORD    PostgreSQL password (default: password)
  SKIP_MINIO           Skip MinIO deployment
  SKIP_UI              Skip Query UI deployment
  SKIP_DATA            Skip HuggingFace dataset loading
EOF
    exit 1
}

# Parse arguments
while getopts "h" opts; do
    case $opts in
        h) usage ;;
        *) usage ;;
    esac
done

# Execute
all

echo -e "\n🌻${GREEN}Trino AI + S3 Lakehouse deployed OK.${NC}🌻\n"
exit 0
