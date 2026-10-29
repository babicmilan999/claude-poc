"""The eval gate (architecture section 10): run the scenarios against the configured
stack and fail when any average score is below its threshold in `evals/thresholds.yaml`.

Skipped by default. Run with `uv run pytest -m eval tests/evals` (`make eval`) after
`make up` (or `make infra` plus the banking service with the `simulation` profile).
Settings come from the environment: `BANKING_API_URL`, `LITELLM_BASE_URL`, `LLM_PROFILE`,
optional `LANGFUSE_*` keys (results then appear as a Langfuse experiment), and the
`EVAL_*` variables in `evals/settings.py`.
"""

import pytest

from core.settings import Settings
from evals.report import check_gate, load_thresholds
from evals.runner import EvalRunner, RunConfig
from evals.runtime import EvalRuntime
from evals.scenario import load_scenarios
from evals.settings import EvalSettings


@pytest.mark.eval
def test_eval_gate() -> None:
    env = EvalSettings()
    scenarios = load_scenarios(env.eval_scenarios_dir, env.eval_only or None)
    runtime = EvalRuntime.from_settings(Settings())
    try:
        report = EvalRunner(runtime).run(
            RunConfig(
                scenarios=scenarios,
                modes=env.eval_modes,
                run_name=env.eval_run_name,
                dataset=env.eval_dataset,
                max_concurrency=env.eval_max_concurrency,
                judge=env.eval_judge,
            )
        )
    finally:
        runtime.tracing.shutdown()
    gate = check_gate(report, load_thresholds(env.eval_thresholds_file))
    print(report.format())
    print(gate.format())
    assert gate.passed, f"{report.format()}\n\n{gate.format()}"
