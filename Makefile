# Makefile for MLB MCP Data Agent
#
# Build and push container images to quay.io/eformat

REGISTRY     ?= quay.io/eformat
AGENT_IMAGE  ?= mlb-agent
MCP_IMAGE    ?= mlb-mcp-server
TAG          ?= latest
PLATFORM     ?= linux/amd64

.PHONY: help build build-agent build-mcp push push-agent push-mcp all \
	deploy-all deploy-agent deploy-mcp restart restart-agent restart-mcp status \
	logs-agent logs-mcp load-data load-baseball load-weather load-pitch load-live upload-data \
	set-model register-prompt spicedb-seed

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-20s\033[0m %s\n", $$1, $$2}'

all: build push ## Build and push all images

build: build-agent build-mcp ## Build all container images

build-agent: ## Build agent image
	podman build --platform $(PLATFORM) \
		-t $(REGISTRY)/$(AGENT_IMAGE):$(TAG) \
		-f agents/mlb-agent/Containerfile \
		agents/mlb-agent

build-mcp: ## Build MCP server image
	podman build --platform $(PLATFORM) \
		-t $(REGISTRY)/$(MCP_IMAGE):$(TAG) \
		-f agents/mlb-mcp-server/Containerfile \
		agents/mlb-mcp-server

push: push-agent push-mcp ## Push all images to registry

push-agent: ## Push agent image
	podman push $(REGISTRY)/$(AGENT_IMAGE):$(TAG)

push-mcp: ## Push MCP server image
	podman push $(REGISTRY)/$(MCP_IMAGE):$(TAG)

# ── Model ───────────────────────────────────────────────────
MAAS_BASE_URL ?= http://maas.apps.ocp.cloud.rhai-tmm.dev/prelude-maas
AGENT_MODEL   ?= qwen36-27b

set-model: ## Switch agent model: make set-model AGENT_MODEL=kimi-k2-6
	@echo "Patching deployment with model $(AGENT_MODEL)..."
	oc set env deployment/mlb-agent \
		MODEL_NAME=$(AGENT_MODEL) \
		MODEL_ENDPOINT=$(MAAS_BASE_URL)/$(AGENT_MODEL)/v1 \
		-n mlb-agent

PROMPT_MSG ?= Prompt update
register-prompt: ## Register system_prompt.md in MLflow: make register-prompt PROMPT_MSG="v3 changes"
	@./scripts/register-prompt.sh "$(PROMPT_MSG)"

NAMESPACE ?= mlb-agent

# ── Deployment ──────────────────────────────────────────────
deploy-all: ## Deploy everything (MinIO → Trino → MCP → Agent)
	./scripts/deploy-all.sh

deploy-agent: ## Deploy/update agent to OpenShift
	oc apply -k agents/mlb-agent/deploy -n $(NAMESPACE)

deploy-mcp: ## Deploy/update MCP server to OpenShift
	oc apply -k deploy/mlb-mcp-server -n $(NAMESPACE)

restart: ## Restart agent + MCP server (picks up new images)
	oc rollout restart deployment/mlb-mcp-server -n $(NAMESPACE)
	oc rollout restart deployment/mlb-agent -n $(NAMESPACE)

restart-agent: ## Restart agent only
	oc rollout restart deployment/mlb-agent -n $(NAMESPACE)

restart-mcp: ## Restart MCP server only
	oc rollout restart deployment/mlb-mcp-server -n $(NAMESPACE)

status: ## Show pods and routes in namespace
	@echo "=== Pods ===" && oc get pods -n $(NAMESPACE) --no-headers | grep -v Completed
	@echo "" && echo "=== Routes ===" && oc get routes -n $(NAMESPACE) -o custom-columns='NAME:.metadata.name,HOST:.spec.host' --no-headers

logs-agent: ## Tail agent logs
	oc logs -f deployment/mlb-agent -n $(NAMESPACE)

logs-mcp: ## Tail MCP server logs
	oc logs -f deployment/mlb-mcp-server -n $(NAMESPACE)

# ── Data Loading ──────────────────────────────────────────
load-data: load-baseball load-weather load-pitch load-live ## Load all data into Trino

load-baseball: ## Load baseball data into Trino (requires port-forward)
	TRINO_HOST=localhost TRINO_PORT=8090 MINIO_ENDPOINT=localhost:9000 \
		DATA_DIR=data/baseball python3 scripts/load-baseball-trino.py

load-weather: ## Load weather data into Trino (requires port-forward)
	TRINO_HOST=localhost TRINO_PORT=8090 MINIO_ENDPOINT=localhost:9000 \
		DATA_DIR=data/weather python3 scripts/load-weather-trino.py

load-pitch: ## Load pitch data into Trino (requires port-forward)
	TRINO_HOST=localhost TRINO_PORT=8090 MINIO_ENDPOINT=localhost:9000 \
		DATA_DIR=data/pitch python3 scripts/load-pitch-trino.py

load-live: ## Load live 2026 season data from MLB Stats API (requires port-forward)
	TRINO_HOST=localhost TRINO_PORT=8090 MINIO_ENDPOINT=localhost:9000 \
		python3 scripts/load-live-trino.py

upload-data: ## Upload raw CSV files to MinIO (requires port-forward)
	MINIO_ENDPOINT=localhost:9000 python3 scripts/upload-data-minio.py

# ── SpiceDB ────────────────────────────────────────────────
spicedb-seed: ## Seed SpiceDB with schema and relationships
	python3 agents/mlb-agent/spicedb/seed_relationships.py
