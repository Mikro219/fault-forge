"""A real model fixes SportsDeck's route, in sealed builds and stacks. Spends inference credits.

Usage: stack_fix_trial.py --budget-usd 1 --key-file PATH --output DIR [--tries 1] [--attempts 3]
"""

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from model_trial import load_key  # noqa: E402

from faultforge import fix_agent  # noqa: E402
from faultforge.fix_agent import ModelPrice, TokenBudget  # noqa: E402
from faultforge.stack import StackSpec, load_stack, run_stack  # noqa: E402
from faultforge.stack_fix import load_fix_config, read_image_file, redact, run_stack_fix_loop  # noqa: E402

DEMO = ROOT / "demo" / "sportsdeck"
CONFIG = DEMO / "faultforge.yaml"
SCENARIO = "refresh-token-race"
MODEL, EFFORT = "glm-5.3", "low"
PRICES = {MODEL: ModelPrice(0.75, 3.00)}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--budget-usd", type=float, required=True)
    parser.add_argument("--key-file", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--tries", type=int, default=1)
    parser.add_argument("--attempts", type=int, default=3, choices=(1, 2, 3))
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    key = load_key(args.key_file)
    config, stack = load_fix_config(DEMO / "fix.yaml"), load_stack(DEMO / "stack.yaml")
    sources = {
        name: redact(read_image_file(config.builder_image, f"/app/{name}"), config.redact)
        for name in (config.editable, *config.context)
    }
    rules = (DEMO / config.business_rules).read_text(encoding="utf-8")
    budget = TokenBudget(args.budget_usd, PRICES)
    log = (args.output / "results.jsonl").open("a", encoding="utf-8")
    passed = 0
    for number in range(1, args.tries + 1):
        project = args.output / f"try-{number}"
        baseline = run_stack(StackSpec(config.base_image, stack, CONFIG), project, SCENARIO)
        if baseline.status != "completed" or baseline.report.verdict != "FAIL":
            print(f"try {number}: the baseline wasn't FAIL: {baseline.status} {baseline.failure_reason}")
            continue

        def on_attempt(record, proposal, built, try_number=number) -> None:
            if proposal.code is not None:
                (args.output / f"try-{try_number}-attempt-{record.attempt}.ts.txt").write_text(proposal.code, encoding="utf-8")
            line = {
                "try": try_number, **record.to_dict(),
                "build_seconds": round(built.seconds, 1) if built else None,
                "build_failure": built.failure_reason if built else None,
            }
            log.write(json.dumps(line) + "\n")
            log.flush()
            regression = [(item.scenario_id, item.passed) for item in record.regression]
            print(
                f"try {try_number} attempt {record.attempt}: verified={record.fix_verified} failure={record.failure_reason} "
                f"verdict={record.verdict} state={record.state_verification} regression={regression} "
                f"model={record.model_wall_seconds} s build={line['build_seconds']} s total={record.wall_seconds} s cost=${record.cost_usd:.4f}",
                flush=True,
            )

        result = run_stack_fix_loop(
            model=MODEL, api_key=key, budget=budget, config=config, sources=sources, rules=rules,
            baseline=baseline.report, baseline_code_sha256=baseline.deployed_code_sha256, stack=stack, config_path=CONFIG,
            project_dir=project, scenario_id=SCENARIO,
            regression_scenarios=fix_agent.regression_scenarios_for(CONFIG, SCENARIO),
            attempts=args.attempts, reasoning_effort=EFFORT, on_attempt=on_attempt,
        )
        passed += result.success
        print(f"try {number}: success={result.success} stop={result.stop_reason} attempts={len(result.attempts)}", flush=True)
    print(f"verified {passed} of {args.tries}; spent ${budget.spent_usd:.4f} of ${args.budget_usd:.2f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
