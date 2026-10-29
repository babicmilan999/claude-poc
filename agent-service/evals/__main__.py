"""`uv run python -m evals`: run the scenarios against the configured stack (banking
service with the `simulation` profile, LiteLLM, optionally Langfuse) and apply the gate.

Exit code 0 when the gate passes, 1 when it fails, 2 on a usage error.
"""

import argparse
import json
import logging
import sys
from pathlib import Path

from core.settings import Settings
from evals.report import check_gate, load_thresholds
from evals.results import Mode
from evals.runner import EvalRunner, RunConfig
from evals.runtime import EvalRuntime
from evals.scenario import ScenarioError, load_scenarios
from evals.settings import EvalSettings


def main(argv: list[str] | None = None) -> int:
    env = EvalSettings()
    parser = argparse.ArgumentParser(prog="python -m evals", description=__doc__)
    parser.add_argument("--only", default=",".join(env.eval_only), help="comma-separated scenario ids")
    parser.add_argument("--modes", default=",".join(m.value for m in env.eval_modes), help="journey,assistant,copilot")
    parser.add_argument("--run-name", default=env.eval_run_name)
    parser.add_argument("--dataset", default=env.eval_dataset)
    parser.add_argument("--scenarios", type=Path, default=env.eval_scenarios_dir)
    parser.add_argument("--thresholds", type=Path, default=env.eval_thresholds_file)
    parser.add_argument("--max-concurrency", type=int, default=env.eval_max_concurrency)
    parser.add_argument("--no-judge", action="store_true", default=not env.eval_judge)
    parser.add_argument("--no-gate", action="store_true", help="report only, always exit 0")
    parser.add_argument("--json", type=Path, help="also write the report as JSON")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    try:
        modes = [Mode(m.strip()) for m in args.modes.split(",") if m.strip()]
        only = [s.strip() for s in args.only.split(",") if s.strip()]
        scenarios = load_scenarios(args.scenarios, only or None)
    except (ValueError, ScenarioError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    runtime = EvalRuntime.from_settings(Settings())
    try:
        report = EvalRunner(runtime).run(
            RunConfig(
                scenarios=scenarios,
                modes=modes,
                run_name=args.run_name,
                dataset=args.dataset,
                max_concurrency=args.max_concurrency,
                judge=not args.no_judge,
            )
        )
    finally:
        runtime.tracing.shutdown()
    print(report.format())
    if args.json:
        args.json.write_text(json.dumps(report.model_dump(mode="json"), indent=2), encoding="utf-8")
    gate = check_gate(report, load_thresholds(args.thresholds))
    print()
    print(gate.format())
    return 0 if gate.passed or args.no_gate else 1


if __name__ == "__main__":
    sys.exit(main())
