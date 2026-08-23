#!/usr/bin/env bash
# Compile and submit an eval pipeline run.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

NAMESPACE="${NAMESPACE:-mlb-agent}"
PIPELINE_FILE="${REPO_DIR}/evaluations/pipelines_gen/mlb-eval-pipeline.yaml"

echo "==> Compiling pipeline..."
python3 "${REPO_DIR}/evaluations/pipeline.py" --compile

DSPA_ROUTE=$(oc get route -n "${NAMESPACE}" -l app=ds-pipeline-dspa -o jsonpath='{.items[0].spec.host}')
SA_TOKEN=$(oc whoami -t)

echo "==> Submitting run..."
python3 -c "
import kfp, warnings
warnings.filterwarnings('ignore')
client = kfp.Client(host='https://${DSPA_ROUTE}', existing_token='${SA_TOKEN}', ssl_ca_cert=None)
client._is_ipython = False

run = client.create_run_from_pipeline_package(
    pipeline_file='${PIPELINE_FILE}',
    run_name='eval-$(date +%Y%m%d-%H%M%S)',
    experiment_name='mlb-data-agent-eval',
    arguments={
        'mlflow_tracking_uri': 'https://mlflow.redhat-ods-applications.svc:8443/mlflow',
        'mlflow_workspace': '${NAMESPACE}',
        'mlflow_experiment_name': 'mlb-data-agent',
        'dataset_name': 'mlb_data_eval',
        'llm_base_url': 'http://maas.apps.ocp.cloud.rhai-tmm.dev/prelude-maas/gemma4/v1',
        'agent_model': 'qwen38-27b',
        'judge_model': 'gemma4',
        'trino_host': 'trino.${NAMESPACE}.svc.cluster.local',
        'trino_port': 8080,
        'llm_secret_name': 'mlb-agent-maas-key',
    },
)
print(f'Run submitted: {run.run_id}')
print(f'View: https://${DSPA_ROUTE}/#/runs/details/{run.run_id}')
"
