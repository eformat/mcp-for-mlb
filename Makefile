# Makefile for MLB MCP Data Agent

REGISTRY     ?= quay.io/eformat
AGENT_IMAGE  ?= mlb-agent
MCP_IMAGE    ?= mlb-mcp-server
TAG          ?= latest
PLATFORM     ?= linux/amd64
NAMESPACE    ?= mlb-agent

.PHONY: help all build push deploy-all deploy-agent deploy-mcp restart status \
	load-data set-model register-prompt spicedb-seed \
	fix-dspa-charset eval-compile eval-submit eval-status

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-20s\033[0m %s\n", $$1, $$2}'

# ── Build & Push ──────────────────────────────────────────
all: build push ## Build and push all images

build: ## Build all container images
	podman build --platform $(PLATFORM) \
		-t $(REGISTRY)/$(AGENT_IMAGE):$(TAG) \
		-f agents/mlb-agent/Containerfile agents/mlb-agent
	podman build --platform $(PLATFORM) \
		-t $(REGISTRY)/$(MCP_IMAGE):$(TAG) \
		-f agents/mlb-mcp-server/Containerfile agents/mlb-mcp-server

push: ## Push all images to registry
	podman push $(REGISTRY)/$(AGENT_IMAGE):$(TAG)
	podman push $(REGISTRY)/$(MCP_IMAGE):$(TAG)

# ── Deploy ────────────────────────────────────────────────
deploy-all: ## Deploy everything from scratch
	./scripts/deploy-all.sh

deploy-agent: ## Deploy/update agent to OpenShift
	oc apply -k agents/mlb-agent/deploy -n $(NAMESPACE)

deploy-mcp: ## Deploy/update MCP server to OpenShift
	oc apply -k deploy/mlb-mcp-server -n $(NAMESPACE)

restart: ## Restart agent + MCP server (picks up new images)
	oc rollout restart deployment/mlb-mcp-server deployment/mlb-agent -n $(NAMESPACE)

status: ## Show pods and routes
	@echo "=== Pods ===" && oc get pods -n $(NAMESPACE) --no-headers | grep -v Completed
	@echo "" && echo "=== Routes ===" && oc get routes -n $(NAMESPACE) -o custom-columns='NAME:.metadata.name,HOST:.spec.host' --no-headers

# ── Data ──────────────────────────────────────────────────
load-data: ## Load data into Trino: make load-data [DATASET=all|baseball|weather|pitch|live|upload]
	./scripts/load-data.sh $(DATASET)

DATASET ?= all

# ── Config ────────────────────────────────────────────────
MAAS_BASE_URL ?= http://maas.apps.ocp.cloud.rhai-tmm.dev/prelude-maas
AGENT_MODEL   ?= qwen36-27b
PROMPT_MSG    ?= Prompt update

set-model: ## Switch agent model: make set-model AGENT_MODEL=kimi-k2-6
	oc set env deployment/mlb-agent \
		MODEL_NAME=$(AGENT_MODEL) \
		MODEL_ENDPOINT=$(MAAS_BASE_URL)/$(AGENT_MODEL)/v1 \
		-n $(NAMESPACE)

register-prompt: ## Register system_prompt.md in MLflow: make register-prompt PROMPT_MSG="v5"
	@./scripts/register-prompt.sh "$(PROMPT_MSG)"

spicedb-seed: ## Seed SpiceDB with schema and relationships
	python3 agents/mlb-agent/spicedb/seed_relationships.py

# ── Evaluation ────────────────────────────────────────────
fix-dspa-charset: ## Fix DSPA MariaDB charset to utf8mb4 (required for KFP)
	./scripts/fix-dspa-charset.sh

eval-compile: ## Compile eval pipeline to YAML
	python3 evaluations/pipeline.py --compile

eval-submit: ## Compile and submit eval pipeline run
	./scripts/eval-submit.sh

eval-status: ## Check latest eval pipeline run status
	@./scripts/eval-status.sh
