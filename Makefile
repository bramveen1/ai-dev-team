.PHONY: compose compose-check up down test lint format add-agent fix-permissions seed-config help

# Use the project venv if it exists, otherwise system python3 — so prod
# machines that haven't set up a venv can still run `make compose` / `make up`.
PYTHON ?= $(if $(wildcard .venv/bin/python),.venv/bin/python,python3)
PYTEST ?= $(PYTHON) -m pytest
RUFF ?= $(PYTHON) -m ruff

compose: ## Render docker-compose.yml from config/agents/*/agent.yaml
	$(PYTHON) -m scripts.render_compose

compose-check: ## Fail if docker-compose.yml is stale relative to the manifests
	$(PYTHON) -m scripts.render_compose --check

up: compose ## Render compose, rebuild images, then docker compose up -d
	# `--build` is load-bearing: `docker compose up -d` only builds when
	# the image is missing, so source changes under a build context
	# (router/, docker/Dockerfile.browser) are silently dropped on
	# subsequent `make up` runs and the container keeps running stale
	# baked code. Uses the layer cache, so the no-op cost is small.
	# The deploy daemon (scripts/deploy-pull.sh) does its own
	# `docker compose build --no-cache` for the same reason — this keeps
	# manual `make up` honest about that contract.
	docker compose up -d --build

down: ## docker compose down
	docker compose down

test: ## Run unit + integration tests
	$(PYTEST) tests/unit -m unit -v
	$(PYTEST) tests/integration -m integration -v

lint: ## ruff check + format check
	$(RUFF) check .
	$(RUFF) format --check .

format: ## ruff format (auto-fix)
	$(RUFF) format .

add-agent: ## Run the add-agent wizard
	$(PYTHON) -m scripts.add_agent

fix-permissions: ## Reset config/agents/*/memory ownership to uid 1000 + 0700/0600 modes (issue #116)
	@scripts/fix_permissions.sh config

seed-config: ## Seed config/ from config.example/ (only fills in missing files; never overwrites)
	@mkdir -p config
	@cp -rn config.example/. config/
	@echo "config/ seeded from config.example/ (existing files preserved)"

help:
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  %-15s %s\n", $$1, $$2}'
