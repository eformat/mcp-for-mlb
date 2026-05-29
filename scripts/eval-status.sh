#!/usr/bin/env bash
# Show latest eval pipeline run statuses.
set -euo pipefail

NAMESPACE="${NAMESPACE:-mlb-agent}"
DSPA_ROUTE=$(oc get route -n "${NAMESPACE}" -l app=ds-pipeline-dspa -o jsonpath='{.items[0].spec.host}')
SA_TOKEN=$(oc whoami -t)

curl -ks "https://${DSPA_ROUTE}/apis/v2beta1/runs?page_size=5&sort_by=created_at%20desc" \
  -H "Authorization: Bearer ${SA_TOKEN}" \
  | python3 -c "
import sys, json
d = json.load(sys.stdin)
for r in d.get('runs', []):
    print(f'  {r[\"display_name\"]:35s} {r[\"state\"]:12s}')
"
