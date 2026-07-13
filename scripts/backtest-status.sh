#!/usr/bin/env bash
# Check latest backtesting pipeline run status.
set -euo pipefail

NAMESPACE="${NAMESPACE:-mlb-agent}"

DSPA_ROUTE=$(oc get route -n "${NAMESPACE}" -l app=ds-pipeline-dspa -o jsonpath='{.items[0].spec.host}')
SA_TOKEN=$(oc whoami -t)

echo "Recent backtesting runs:"
curl -ks "https://${DSPA_ROUTE}/apis/v2beta1/runs?page_size=5&sort_by=created_at%20desc&filter=%7B%22predicates%22%3A%5B%7B%22key%22%3A%22name%22%2C%22operation%22%3A%22IS_SUBSTRING%22%2C%22string_value%22%3A%22backtest%22%7D%5D%7D" \
  -H "Authorization: Bearer ${SA_TOKEN}" \
  | python3 -c "
import sys, json
d = json.load(sys.stdin)
runs = d.get('runs', [])
if not runs:
    print('  No backtesting runs found.')
else:
    for r in runs:
        print(f'  {r[\"display_name\"]:35s} {r[\"state\"]:12s}  {r.get(\"created_at\", \"\")[:19]}')
"
