.PHONY: compose compose-check up down test lint format add-agent help

# Use the project venv if it exists, otherwise system python3 — so prod
# machines that haven't set up a venv can still run `make compose` / `make up`.
PYTHON ?= $(if $(wildcard .venv/bin/python),.venv/bin/python,python3)

compose: ## Render docker-compose.yml from config/agents/*/agent.yaml
	$(PYTHON) -m scripts.render_compose

compose-check: ## Fail if docker-compose.yml is stale relative to the manifests
	$(PYTHON) -m scripts.render_compose --check

up: compose ## Render compose, then docker compose up -d
	docker compose up -d

down: ## docker compose down
	docker compose down

test: ## Run unit + integration tests
	.venv/bin/pytest tests/unit -m unit -v
	.venv/bin/pytest tests/integration -m integration -v

lint: ## ruff check + format check
	.venv/bin/ruff check .
	.venv/bin/ruff format --check .

format: ## ruff format (auto-fix)
	.venv/bin/ruff format .

add-agent: ## Run the add-agent wizard
	$(PYTHON) -m scripts.add_agent

help:
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  %-15s %s\n", $$1, $$2}'
