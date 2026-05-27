# Makefile for MLB MCP Data Agent
#
# Build and push container images to quay.io/eformat

REGISTRY     ?= quay.io/eformat
AGENT_IMAGE  ?= mlb-agent
MCP_IMAGE    ?= mlb-mcp-server
TAG          ?= latest
PLATFORM     ?= linux/amd64

.PHONY: help build build-agent build-mcp push push-agent push-mcp all deploy-all spicedb-schema spicedb-seed spicedb-check

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

# ── Deployment ──────────────────────────────────────────────
deploy-all: ## Deploy everything (MinIO → Trino → MCP → Agent)
	./scripts/deploy-all.sh

# ── SpiceDB ────────────────────────────────────────────────
spicedb-seed: ## Seed SpiceDB with schema and relationships
	python3 agents/mlb-agent/spicedb/seed_relationships.py

USER ?= admin
PERM ?= query
DATASET ?= batting
spicedb-check: ## Check permission: make spicedb-check USER=admin PERM=query DATASET=batting
	@python3 -c "from authzed.api.v1 import Client; from grpcutil import insecure_bearer_token_credentials; \
		c=Client('localhost:50051', insecure_bearer_token_credentials('averysecretpresharedkey')); \
		print(c.CheckPermission(...))"
