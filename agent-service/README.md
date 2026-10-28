# agent-service

FastAPI app with the LangGraph graphs, tools, investigation workers, simulator and evals.
Design: `docs/architecture.md`; API: `contracts/agent-api.yaml`. Settings come from
environment variables (documented in the root `.env.example`); the app does not read a
`.env` file itself.

## Run natively

Needs Python 3.12 and [uv](https://docs.astral.sh/uv/), plus the services it talks to:
Postgres with pgvector, the banking service, LiteLLM (and Langfuse if you want traces).
`make infra` from the repo root starts them in Docker.

```bash
cd agent-service
uv sync                                   # install dependencies (incl. dev tools)

export DATABASE_URL=postgresql://app:app@localhost:5432/app
export BANKING_API_URL=http://localhost:8080
export LITELLM_BASE_URL=http://localhost:4000

uv run alembic upgrade head               # apply migrations (agents and kb schemas)
uv run uvicorn main:app --reload --port 8000
```

Migrations also run at startup (`MIGRATE_ON_STARTUP=true`, the default) when
`DATABASE_URL` is set, before the knowledge base is ingested; the manual command above is
for when you turn that off or want to migrate without starting the app. To migrate a
different database: `uv run alembic -x url=postgresql://... upgrade head`.

The knowledge base is read from `KB_PATH` (default `../knowledge-base`).

## Tests

```bash
uv run pytest                # unit tests: no Docker, Postgres, Ollama or API keys
uv run ruff check .
uv run mypy .
```

`make test-agent` from the repo root runs the unit tests.

Integration tests need a real Postgres with pgvector. They drop and recreate the `agents`
and `kb` schemas, so point them at a throwaway database:

```bash
TEST_DATABASE_URL=postgresql://app:app@localhost:5432/app_test uv run pytest -m integration
```

## Docker

`infra/docker-compose.yml` builds this folder's `Dockerfile` (`make up`). The container
serves `uvicorn main:app` on `0.0.0.0:8000` and reads the knowledge base from
`/app/knowledge-base`, which Compose mounts from `../knowledge-base`.
