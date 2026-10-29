"""The eval gate's pytest setup.

- Registers the `eval` marker. Eval tests run against the real stack (banking service
  with the `simulation` profile, LiteLLM, optionally Langfuse) and are skipped unless the
  marker is selected: `uv run pytest -m eval tests/evals`.
- Overrides the suite's autouse `_no_real_services` fixture for this folder only, so the
  gate reads the real service settings from the environment. Unit tests elsewhere keep it.
"""

import pytest


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers", "eval: runs the eval scenarios against the real stack; skipped unless selected with -m eval"
    )


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    if "eval" in (config.getoption("markexpr") or ""):
        return
    skip = pytest.mark.skip(reason="eval gate: run with `uv run pytest -m eval tests/evals` (needs the full stack)")
    for item in items:
        if item.get_closest_marker("eval") is not None:
            item.add_marker(skip)


@pytest.fixture(autouse=True)
def _no_real_services() -> None:
    """The gate needs the real BANKING_API_URL, LITELLM_BASE_URL and Langfuse keys."""
