#!/usr/bin/env bash
#
# Fix DSPA MariaDB charset to utf8mb4 for KFP pipeline storage.
#
# KFP's compiled pipeline YAML contains UTF-8 box-drawing characters
# that MariaDB's default latin1 charset rejects. This script converts
# all tables to utf8mb4 after DSPA deployment.
#
# Usage:
#   ./scripts/fix-dspa-charset.sh
#
set -euo pipefail

NAMESPACE="${NAMESPACE:-mlb-agent}"

MARIA_POD=$(oc get pod -n "$NAMESPACE" -l app=mariadb-dspa -o jsonpath='{.items[0].metadata.name}')
echo "MariaDB pod: $MARIA_POD"

echo "Converting mlpipeline database to utf8mb4..."
oc exec "$MARIA_POD" -n "$NAMESPACE" -- mysql -u root mlpipeline -e "
SET FOREIGN_KEY_CHECKS=0;

-- Drop all foreign keys
ALTER TABLE pipeline_tags DROP FOREIGN KEY pipeline_tags_PipelineId_pipelines_UUID_foreign;
ALTER TABLE pipeline_versions DROP FOREIGN KEY pipeline_versions_PipelineId_pipelines_UUID_foreign;
ALTER TABLE pipeline_version_tags DROP FOREIGN KEY pv_tags_PipelineVersionId_pv_UUID_fk;
ALTER TABLE run_metrics DROP FOREIGN KEY run_metrics_RunUUID_run_details_UUID_foreign;
ALTER TABLE tasks DROP FOREIGN KEY tasks_RunUUID_run_details_UUID_foreign;

-- Convert all tables
ALTER TABLE pipelines CONVERT TO CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
ALTER TABLE pipeline_versions CONVERT TO CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
ALTER TABLE pipeline_tags CONVERT TO CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
ALTER TABLE pipeline_version_tags CONVERT TO CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
ALTER TABLE run_details CONVERT TO CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
ALTER TABLE run_metrics CONVERT TO CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
ALTER TABLE tasks CONVERT TO CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
ALTER TABLE experiments CONVERT TO CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
ALTER TABLE jobs CONVERT TO CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
ALTER TABLE resource_references CONVERT TO CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
ALTER TABLE default_experiments CONVERT TO CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
ALTER TABLE db_statuses CONVERT TO CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;

-- Recreate foreign keys
ALTER TABLE pipeline_tags ADD CONSTRAINT pipeline_tags_PipelineId_pipelines_UUID_foreign FOREIGN KEY (PipelineId) REFERENCES pipelines(UUID);
ALTER TABLE pipeline_versions ADD CONSTRAINT pipeline_versions_PipelineId_pipelines_UUID_foreign FOREIGN KEY (PipelineId) REFERENCES pipelines(UUID);
ALTER TABLE pipeline_version_tags ADD CONSTRAINT pv_tags_PipelineVersionId_pv_UUID_fk FOREIGN KEY (PipelineVersionId) REFERENCES pipeline_versions(UUID);
ALTER TABLE run_metrics ADD CONSTRAINT run_metrics_RunUUID_run_details_UUID_foreign FOREIGN KEY (RunUUID) REFERENCES run_details(UUID);
ALTER TABLE tasks ADD CONSTRAINT tasks_RunUUID_run_details_UUID_foreign FOREIGN KEY (RunUUID) REFERENCES run_details(UUID);

ALTER DATABASE mlpipeline CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
SET FOREIGN_KEY_CHECKS=1;
"

echo "Restarting DSPA pod to pick up charset change..."
oc delete pod -l app=ds-pipeline-dspa -n "$NAMESPACE"
echo "Waiting for DSPA..."
sleep 10
oc rollout status deployment/ds-pipeline-dspa -n "$NAMESPACE" --timeout=120s

echo "Done. MariaDB charset: utf8mb4"
