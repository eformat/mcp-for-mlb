# Trino on OpenShift — AI Functions + S3 Lakehouse Setup Guide

This guide sets up Trino on OpenShift with:

- AI functions powered by an OpenAI-compatible LLM endpoint
- Iceberg lakehouse on MinIO S3 (via Nessie catalog)
- HuggingFace hotel reviews dataset for demo queries

## Prerequisites

- OpenShift cluster with `oc` CLI authenticated
- `helm` >= 3
- `mc` (MinIO client)
- `trino` CLI
- Python 3 with `pip`
- `hf` CLI (HuggingFace)

Download the trino `trino-cli` client and make it executable in your $PATH:

- https://github.com/trinodb/trino/releases

Download the minio `mc` client and make it executable in your $PATH:

- https://github.com/minio/mc/releases

Install `hf` using `pip install hf`

Download `helm` and make it executable in your $PATH:

- https://github.com/helm/helm/releases

## 1. Deploy MinIO

```bash
oc apply -k ~/git/openshift-minio/overlays/cluster-dev
```

Default credentials: `minio / minio1234` (change for production)

## 2. Deploy Trino

### Fix OpenShift SCC compatibility

In `trino/values.yaml`, set the pod security context to let OpenShift assign UIDs:

```yaml
securityContext:
  runAsNonRoot: true
```

Add writable data volumes for both coordinator and worker:

```yaml
coordinator:
  additionalVolumes:
    - name: data
      emptyDir: {}
  additionalVolumeMounts:
    - name: data
      mountPath: /data/trino

worker:
  additionalVolumes:
    - name: data
      emptyDir: {}
  additionalVolumeMounts:
    - name: data
      mountPath: /data/trino
```

Enable forwarded headers (required for OpenShift Routes):

```yaml
additionalConfigProperties:
  - http-server.process-forwarded=true
  - sql.path=llm.ai
```

### Add the LLM catalog

You need access to a non-thinking LLM. Some of the trino functions may fail on `<think>` tokens.

Using a MaaS:

```yaml
catalogs:
  llm: |
    connector.name=ai
    ai.provider=openai
    ai.model=llama-4-scout-17b-16e-w4a16
    ai.openai.endpoint=https://maas.apps.ocp.cloud.rhai-tmm.dev/prelude-maas/llama-4-scout-17b-16e-w4a16
    ai.openai.api-key=${ENV:OPENAI_API_KEY}
```

### Create the secrets and deploy

```bash
oc create secret generic trino-llm-api-key -n trino \
  --from-literal=OPENAI_API_KEY='<your-api-key>' \
  --from-literal=S3_ACCESS_KEY='<s3-access-key>' \
  --from-literal=S3_SECRET_KEY='<s3-secret-key>'
```

```bash
helm install trino ./trino -n trino --create-namespace
```

Add `envFrom` in values.yaml to inject the secret:

```yaml
envFrom:
  - secretRef:
      name: trino-llm-api-key
```

### Add OpenShift Route

Create `trino/templates/route.yaml` for external access, then upgrade:

```bash
helm upgrade trino ./trino -n trino
```

### Verify

```bash
oc get pods -n trino
oc -n trino port-forward svc/trino 8080:8080
```

```bash
trino --server http://localhost:8080 --execute "SELECT ai_gen('Say hello')"
```

## 3. Deploy Nessie Catalog Server

Nessie provides lightweight Iceberg catalog metadata management — no external database needed.

```bash
oc apply -f nessie/ -n trino
```

Verify:

```bash
oc get pods -n trino -l app=nessie
```

## 4. Create MinIO Bucket

```bash
oc -n minio port-forward svc/minio 9000:9000 &
mc alias set myminio http://localhost:9000 <s3-access-key> <s3-secret-key>
mc mb myminio/warehouse
```

## 5. Add Iceberg+S3 Catalog to Trino

Add to the `catalogs` section in `trino/values.yaml`:

```yaml
  lakehouse: |
    connector.name=iceberg
    iceberg.catalog.type=nessie
    iceberg.nessie-catalog.uri=http://nessie.trino.svc.cluster.local:19120/api/v2
    iceberg.nessie-catalog.default-warehouse-dir=s3://warehouse/
    fs.native-s3.enabled=true
    s3.endpoint=http://minio.minio.svc.cluster.local:9000
    s3.region=us-east-1
    s3.path-style-access=true
    s3.aws-access-key=${ENV:S3_ACCESS_KEY}
    s3.aws-secret-key=${ENV:S3_SECRET_KEY}
```

Note: Trino 480 uses `fs.native-s3.enabled=true`. Trino 481+ uses `fs.s3.enabled=true`.

Deploy:

```bash
helm upgrade trino ./trino -n trino
```

Verify:

```bash
trino --server http://localhost:8080 --execute "SHOW CATALOGS"
# Should list: lakehouse, llm, system, tpcds, tpch
```

## 6. Load HuggingFace Dataset

### Download

```bash
export HF_TOKEN=<your-hf-token>
hf download Aditya1010/17k-hotel-reviews-dataset --repo-type dataset --local-dir /tmp/hotel-reviews
```

### Install Python dependencies

```bash
python3.12 -m venv venv
source venv/bin/activate
```

```bash
uv pip install trino pandas pyarrow
```

### Load into Trino

Ensure port-forward is active

```bash
oc -n trino port-forward svc/trino 8080:8080
```

then:

```bash
python examples/load_dataset.py
```

This creates the `lakehouse.reviews.hotel_reviews` Iceberg table on MinIO S3 with ~17,840 hotel reviews.

### Verify

```bash
trino --server http://localhost:8080 --execute \
  "SELECT label, count(*) FROM lakehouse.reviews.hotel_reviews GROUP BY label"
# NEGATIVE  8920
# POSITIVE  8920
```

## 7. Load Financial Sentiment Dataset

### Download

```bash
export HF_TOKEN=<your-hf-token>
hf download NOSIBLE/financial-sentiment --repo-type dataset --local-dir /tmp/financial-sentiment
```

### Load into Trino

```bash
python examples/load_financial_sentiment.py
```

This creates the `lakehouse.finance.financial_news` Iceberg table with 100,000 financial news articles.

### Verify

```bash
trino --server http://localhost:8080 --execute \
  "SELECT label, count(*) FROM lakehouse.finance.financial_news GROUP BY label"
# neutral   39309
# positive  36257
# negative  24434
```

## 8. Deploy Trino Query UI (Optional)

Build and push the container image:

- https://github.com/eformat/trino-query-ui

```bash
cd ~/git/trino-query-ui
podman build -t quay.io/eformat/trino-query-ui:latest -f Containerfile .
podman push quay.io/eformat/trino-query-ui:latest
```

Deploy the Helm chart:

```bash
helm install trino-query-ui chart/ -n trino
```

## 9. Run AI Function Examples

### Smoke test

```bash
oc -n trino port-forward svc/trino 8080:8080
```

```bash
trino --server http://localhost:8080
SELECT llm.ai.ai_gen('Say hello');
```

### Self-contained examples (01-18)

These use inline data and cover all 7 AI functions:

```bash
trino --server http://localhost:8080 -f examples/04_classify_firewall_logs.sql
```

### Lakehouse examples (19-21)

These query the hotel reviews dataset on MinIO S3:

```bash
# Review intelligence: sentiment + classify + extract
trino --server http://localhost:8080 -f examples/19_reviews_intelligence.sql

# Executive summary from negative reviews
trino --server http://localhost:8080 -f examples/20_reviews_summary.sql

# PII masking + grammar fix + translation
trino --server http://localhost:8080 -f examples/21_reviews_pii_safe.sql
```

### Financial sentiment examples (22-26)

These query 100K financial news articles on MinIO S3:

```bash
# Cross-validate AI vs human sentiment labels
trino --server http://localhost:8080 -f examples/22_finance_mood_ring.sql

# Classify risk type, extract entities, mask PII
trino --server http://localhost:8080 -f examples/23_finance_threat_intel.sql

# Translate financial news to Japanese and Spanish
trino --server http://localhost:8080 -f examples/24_finance_multilingual.sql

# Editorial pipeline: grammar fix + headline generation + sector classification
trino --server http://localhost:8080 -f examples/25_finance_editorial.sql

# Morning briefing: aggregate negative news into analyst report
trino --server http://localhost:8080 -f examples/26_finance_daily_briefing.sql
```

### Run all examples

```bash
./examples/run_all.sh
```

## Architecture

```
┌──────────────┐     ┌──────────────┐     ┌──────────────┐
│  Trino Query │     │    Trino     │     │   LLM (MaaS) │
│     UI       │────▶│ Coordinator  │────▶│ Llama 4 Scout│
└──────────────┘     │  + Workers   │     └──────────────┘
                     └──────┬───────┘
                            │
               ┌────────────┼────────────┐
               │            │            │
        ┌──────▼──┐  ┌──────▼──┐  ┌──────▼──┐
        │  Nessie │  │  MinIO  │  │  TPC-H  │
        │ Catalog │  │   S3    │  │ TPC-DS  │
        └─────────┘  └─────────┘  └─────────┘
```
