# Makefile for MLB MCP Data Agent

REGISTRY     ?= quay.io/eformat
AGENT_IMAGE  ?= mlb-agent
MCP_IMAGE    ?= mlb-mcp-server
TAG          ?= latest
PLATFORM     ?= linux/amd64
NAMESPACE    ?= mlb-agent

.PHONY: help all build push deploy-all deploy-agent deploy-mcp deploy-hermes restart status \
	load-data lakehouse-summary check-games set-model register-prompt spicedb-seed \
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

deploy-hermes: ## Deploy/upgrade Hermes agent with MLB Kanban picker
	helm upgrade --install hermes deploy/hermes-chart \
		--namespace $(HERMES_NAMESPACE) \
		--set config.model.provider=custom \
		--set config.model.model=$(AGENT_MODEL) \
		--set config.model.base_url=$(MAAS_BASE_URL)/$(AGENT_MODEL)/v1 \
		--set config.model.temperature=0.3 \
		--set 'config.model.api_key=$${OPENAI_API_KEY}' \
		--set secretEnv.OPENAI_API_KEY=$${OPENAI_API_KEY}

restart: ## Restart agent + MCP server (picks up new images)
	oc rollout restart deployment/mlb-mcp-server deployment/mlb-agent -n $(NAMESPACE)

status: ## Show pods and routes
	@echo "=== Pods ===" && oc get pods -n $(NAMESPACE) --no-headers | grep -v Completed
	@echo "" && echo "=== Routes ===" && oc get routes -n $(NAMESPACE) -o custom-columns='NAME:.metadata.name,HOST:.spec.host' --no-headers

# ── Data ──────────────────────────────────────────────────
load-data: ## Load data into Trino: make load-data [DATASET=all|baseball|weather|pitch|live|predictions|upload]
	./scripts/load-data.sh $(DATASET)

load-predictions: ## Load prediction history from Chainlit into lakehouse
	./scripts/load-data.sh predictions

HERMES_NAMESPACE ?= hermes

load-kanban-predictions: ## Load predictions from Hermes Kanban into lakehouse
	@POD=$$(oc get pods -n $(HERMES_NAMESPACE) -l app.kubernetes.io/name=hermes \
	  -o jsonpath='{.items[0].metadata.name}') && \
	echo "Copying kanban.db from $$POD..." && \
	mkdir -p data/predictions && \
	oc cp $(HERMES_NAMESPACE)/$$POD:/opt/data/kanban.db data/predictions/kanban.db && \
	./scripts/load-data.sh kanban-predictions

restore-history: ## Restore Chainlit chat history from data/predictions/chainlit.db
	@POD=$$(oc get pods -n $(NAMESPACE) -l app.kubernetes.io/name=mlb-agent -o jsonpath='{.items[0].metadata.name}') && \
	echo "Copying chainlit.db to $$POD..." && \
	oc cp data/predictions/chainlit.db $(NAMESPACE)/$$POD:/app/data/chainlit.db && \
	echo "Restarting agent..." && \
	oc rollout restart deployment/mlb-agent -n $(NAMESPACE)

lakehouse-summary: ## Show tables and row counts in the lakehouse
	./scripts/lakehouse-summary.sh

check-games: ## Check game statuses: make check-games [DATE=2026-05-28]
	@./scripts/check-games.sh $(DATE)

DATASET ?= all
DATE    ?=

# ── Config ────────────────────────────────────────────────
MAAS_BASE_URL ?= https://maas.apps.ocp.cloud.rhai-tmm.dev/prelude-maas
AGENT_MODEL   ?= qwen38-27b
PROMPT_MSG    ?= Prompt update

set-model: ## Switch agent model: make set-model AGENT_MODEL=kimi-k2-6
	oc set env deployment/mlb-agent \
		MODEL_NAME=$(AGENT_MODEL) \
		MODEL_ENDPOINT=$(MAAS_BASE_URL)/$(AGENT_MODEL)/v1 \
		-n $(NAMESPACE)

register-prompt: ## Register system_prompt.md in MLflow: make register-prompt PROMPT_MSG="v5"
	@./scripts/register-prompt.sh "$(PROMPT_MSG)"

spicedb-seed: ## Seed SpiceDB with schema and relationships
	@bash -c 'POD=$$(oc get pods -n $(NAMESPACE) -l app.kubernetes.io/instance=dev-spicedb -o jsonpath="{.items[0].metadata.name}") && \
	oc port-forward pod/$$POD -n $(NAMESPACE) 50051:50051 &>/dev/null & PF=$$!; \
	sleep 3 && .venv/bin/python agents/mlb-agent/spicedb/seed_relationships.py; RC=$$?; \
	kill $$PF 2>/dev/null; exit $$RC'

# ── Evaluation ────────────────────────────────────────────
fix-dspa-charset: ## Fix DSPA MariaDB charset to utf8mb4 (required for KFP)
	./scripts/fix-dspa-charset.sh

eval-compile: ## Compile eval pipeline to YAML
	python3 evaluations/pipeline.py --compile

eval-submit: ## Compile and submit eval pipeline run
	./scripts/eval-submit.sh

eval-status: ## Check latest eval pipeline run status
	@./scripts/eval-status.sh

# ── Prompt Tuning ────────────────────────────────────────
tune-prompt: ## Run RL prompt tuning loop locally
	./scripts/tune-prompt.sh --max-steps 50 --batch-size 40

tune-prompt-dry: ## Dry run: show baseline metrics without modifying prompts
	./scripts/tune-prompt.sh --dry-run

tune-compile: ## Compile prompt tuning pipeline to YAML
	python3 prompt_tuning/pipeline.py --compile

tune-submit: ## Compile and submit prompt tuning pipeline run
	./scripts/tune-submit.sh

tune-backtest-submit: ## Tune against backtest_results (542+ games)
	./scripts/tune-backtest-submit.sh

tune-status: ## Check latest prompt tuning pipeline run status
	@./scripts/tune-status.sh

# ── Backtesting ──────────────────────────────────────────
backtest-compile: ## Compile backtesting pipeline to YAML
	python3 backtesting/pipeline.py --compile

backtest-submit: ## Submit backtesting pipeline run
	./scripts/backtest-submit.sh

backtest-status: ## Check latest backtesting pipeline run status
	@./scripts/backtest-status.sh
