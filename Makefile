# Root entry points. Targets for apps that do not exist yet print "skipped".

COMPOSE := docker compose -f infra/docker-compose.yml --env-file .env
# Services `make infra` starts, so an app can run natively against them.
INFRA_SERVICES ?= postgres litellm langfuse-web langfuse-worker
REDOCLY := npx --yes @redocly/cli@1.34.3
AJV := npx --yes ajv-cli@5.0.0

.PHONY: help test test-agent test-banking test-frontend lint-contracts bundle-contracts up down infra models

help:
	@echo "make test            run every app's tests"
	@echo "make test-agent      agent-service tests (uv + pytest)"
	@echo "make test-banking    banking-service tests (Maven wrapper)"
	@echo "make test-frontend   frontend tests (npm)"
	@echo "make lint-contracts  lint OpenAPI specs, validate scenario examples"
	@echo "make bundle-contracts  single-file specs for code generators (contracts/dist/)"
	@echo "make up / down       start / stop the full stack in Docker Compose"
	@echo "make infra           start only Postgres, LiteLLM and Langfuse"
	@echo "make models          pull the Ollama models of LLM_PROFILE"

test: test-agent test-banking test-frontend

test-agent:
	@if [ -f agent-service/pyproject.toml ]; then \
		cd agent-service && uv run pytest; \
	else echo "test-agent: skipped (agent-service not built yet)"; fi

test-banking:
	@if [ -f banking-service/pom.xml ]; then \
		cd banking-service && ./mvnw -B -q test; \
	else echo "test-banking: skipped (banking-service not built yet)"; fi

test-frontend:
	@if [ -f frontend/package-lock.json ]; then \
		cd frontend && { [ -d node_modules ] || npm ci; } && npm test; \
	else echo "test-frontend: skipped (frontend not built yet)"; fi

lint-contracts:
	cd contracts && $(REDOCLY) lint banking-api.yaml agent-api.yaml
	cd contracts && $(AJV) validate --spec=draft2020 -s scenario.schema.json -d "examples/scenarios/*.yaml"

# Resolves the $refs into scenario.schema.json, for generators with weak OpenAPI 3.1 support.
bundle-contracts:
	cd contracts && $(REDOCLY) bundle banking-api.yaml -o dist/banking-api.yaml
	cd contracts && $(REDOCLY) bundle agent-api.yaml -o dist/agent-api.yaml

up:
	@if [ -f infra/docker-compose.yml ]; then $(COMPOSE) up -d --build; \
	else echo "up: skipped (infra/docker-compose.yml not written yet)"; fi

down:
	@if [ -f infra/docker-compose.yml ]; then $(COMPOSE) down; \
	else echo "down: skipped (infra/docker-compose.yml not written yet)"; fi

infra:
	@if [ -f infra/docker-compose.yml ]; then $(COMPOSE) up -d $(INFRA_SERVICES); \
	else echo "infra: skipped (infra/docker-compose.yml not written yet)"; fi

models:
	@if [ -x infra/scripts/pull-models.sh ]; then infra/scripts/pull-models.sh "$${LLM_PROFILE:-ollama}"; \
	else echo "models: skipped (infra/scripts/pull-models.sh not written yet)"; fi
