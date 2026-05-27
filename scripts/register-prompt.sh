#!/usr/bin/env bash
# Register the current system_prompt.md as a new version in MLflow.
# Usage: ./scripts/register-prompt.sh [commit-message]
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
PROMPT_FILE="${REPO_DIR}/agents/mlb-agent/system_prompt.md"
NAMESPACE="${NAMESPACE:-mlb-agent}"
COMMIT_MSG="${1:-Prompt update}"

if [ ! -f "${PROMPT_FILE}" ]; then
  echo "ERROR: ${PROMPT_FILE} not found"
  exit 1
fi

echo "Registering prompt from: ${PROMPT_FILE}"
echo "Commit message: ${COMMIT_MSG}"

# Kill any existing port-forward on 8443
kill $(lsof -ti:8443) 2>/dev/null || true
sleep 1

oc port-forward svc/mlflow -n redhat-ods-applications 8443:8443 &
PF_PID=$!
sleep 3

SA_TOKEN=$(oc whoami -t)

python3 -c "
import os, warnings
warnings.filterwarnings('ignore')
os.environ['MLFLOW_TRACKING_INSECURE_TLS'] = 'true'
os.environ['MLFLOW_TRACKING_TOKEN'] = '${SA_TOKEN}'

import mlflow
mlflow.set_tracking_uri('https://localhost:8443/mlflow')
mlflow.set_workspace('${NAMESPACE}')

content = open('${PROMPT_FILE}').read()
result = mlflow.genai.register_prompt(
    name='mlb-agent.system',
    template=content,
    commit_message='${COMMIT_MSG}',
    tags={'agent': 'system', 'source': 'system_prompt.md'},
)
print(f'Registered: mlb-agent.system v{result.version} ({len(content)} chars)')
mlflow.genai.set_prompt_alias('mlb-agent.system', alias='production', version=result.version)
print(f'Alias @production -> v{result.version}')
"

kill $PF_PID 2>/dev/null || true
