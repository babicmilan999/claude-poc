# Agent service: FastAPI + LangGraph graphs + investigation workers (one container for the POC).
# Built by infra/docker-compose.yml (context: ../agent-service). The knowledge base is not in
# the build context; Compose mounts ../knowledge-base at /app/knowledge-base (KB_PATH).

FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:$PATH" \
    KB_PATH=/app/knowledge-base

WORKDIR /app

# Dependencies first (cached layer), from the lock file only.
COPY pyproject.toml uv.lock .python-version ./
RUN uv sync --frozen --no-dev --no-install-project

# Application code: core/, migrations/, main.py, alembic.ini and any feature packages
# (assistant/, investigation/, simulation/, evals/); see .dockerignore for what is left out.
COPY . .
RUN uv sync --frozen --no-dev

RUN useradd --create-home --uid 10001 app \
    && mkdir -p /app/knowledge-base \
    && chown -R app:app /app
USER app

EXPOSE 8000

# Migrations run in the app's startup (MIGRATE_ON_STARTUP, default true), before KB ingestion.
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
