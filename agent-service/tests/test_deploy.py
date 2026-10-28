"""The container matches `infra/docker-compose.yml` (Docker itself is not needed)."""

from pathlib import Path

import pytest
import yaml

from core.settings import AGENT_SERVICE_DIR, Settings

COMPOSE = AGENT_SERVICE_DIR.parent / "infra" / "docker-compose.yml"
DOCKERFILE = AGENT_SERVICE_DIR / "Dockerfile"


@pytest.fixture
def compose_service() -> dict:
    if not COMPOSE.exists():
        pytest.skip("infra/docker-compose.yml not present")
    return yaml.safe_load(COMPOSE.read_text())["services"]["agent-service"]


def test_every_compose_variable_is_a_setting(compose_service: dict) -> None:
    fields = {name.upper() for name in Settings.model_fields}
    assert set(compose_service["environment"]) <= fields


def test_dockerfile_serves_main_app_on_port_8000_with_the_mounted_kb(compose_service: dict) -> None:
    dockerfile = DOCKERFILE.read_text()
    assert 'CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]' in dockerfile
    assert "uv sync --frozen --no-dev" in dockerfile
    assert "8000:8000" in compose_service["ports"]
    kb_path = compose_service["environment"]["KB_PATH"]
    assert any(v.split(":")[1] == kb_path for v in compose_service["volumes"])
    assert f"KB_PATH={kb_path}" in dockerfile


def test_dockerignore_keeps_local_state_out() -> None:
    ignored = Path(AGENT_SERVICE_DIR / ".dockerignore").read_text().split()
    assert {".venv/", ".env"} <= set(ignored)
