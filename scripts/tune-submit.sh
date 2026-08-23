#!/usr/bin/env bash
# Compile and submit a prompt tuning pipeline run.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

NAMESPACE="${NAMESPACE:-mlb-agent}"
VENV="${REPO_DIR}/.venv"
PYTHON="${VENV}/bin/python"
PIPELINE_FILE="${REPO_DIR}/prompt_tuning/pipelines_gen/mlb-tune-pipeline.yaml"

# Ensure GCP ADC secret exists for Claude meta-agent
GCP_ADC_SECRET="${GCP_ADC_SECRET:-gcp-adc}"
ADC_FILE="${HOME}/.config/gcloud/application_default_credentials.json"
if [ -f "$ADC_FILE" ]; then
    oc create secret generic "$GCP_ADC_SECRET" \
        --from-file=application_default_credentials.json="$ADC_FILE" \
        -n "$NAMESPACE" --dry-run=client -o yaml | oc apply -f - -n "$NAMESPACE" > /dev/null
    echo "==> GCP ADC secret '${GCP_ADC_SECRET}' ensured"
else
    echo "WARN: ${ADC_FILE} not found — Claude meta-agent won't work without it"
fi

echo "==> Compiling pipeline..."
$PYTHON "${REPO_DIR}/prompt_tuning/pipeline.py" --compile

DSPA_ROUTE=$(oc get route -n "${NAMESPACE}" -l app=ds-pipeline-dspa -o jsonpath='{.items[0].spec.host}')
SA_TOKEN=$(oc whoami -t)

echo "==> Submitting run..."
$PYTHON -c "
import kfp, warnings
warnings.filterwarnings('ignore')
client = kfp.Client(host='https://${DSPA_ROUTE}', existing_token='${SA_TOKEN}', ssl_ca_cert=None)
client._is_ipython = False

run = client.create_run_from_pipeline_package(
    pipeline_file='${PIPELINE_FILE}',
    run_name='tune-$(date +%Y%m%d-%H%M%S)',
    experiment_name='mlb-prompt-tuning',
    arguments={
        'mlflow_tracking_uri': 'https://mlflow.redhat-ods-applications.svc:8443/mlflow',
        'mlflow_workspace': '${NAMESPACE}',
        'mlflow_experiment_name': 'mlb-prompt-tuning',
        'agent_model': '${AGENT_MODEL:-qwen38-27b}',
        'llm_base_url': 'https://maas.apps.ocp.cloud.rhai-tmm.dev/prelude-maas/${AGENT_MODEL:-qwen38-27b}/v1',
        'trino_host': 'trino.${NAMESPACE}.svc.cluster.local',
        'trino_port': 8080,
        'max_steps': ${MAX_STEPS:-50},
        'eval_batch_size': ${BATCH_SIZE:-40},
        'accuracy_target': ${ACCURACY_TARGET:-0.65},
        'llm_secret_name': 'mlb-agent-maas-key',
        'meta_model': '${META_MODEL:-claude-sonnet-4-6}',
        'gcp_adc_secret_name': '${GCP_ADC_SECRET:-gcp-adc}',
    },
)
print(f'Run submitted: {run.run_id}')
print(f'View: https://${DSPA_ROUTE}/#/runs/details/{run.run_id}')
"
