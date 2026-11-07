"""A real model fixes SportsDeck's route, in sealed builds and stacks. Spends inference credits.

Usage: stack_fix_trial.py --budget-usd 1 --key-file PATH --output DIR [--tries 1] [--attempts 3] [--record DIR]

With --record, every try that started writes a replay, whatever its outcome, so that a page showing
them can't be showing only the good ones.
"""

import argparse
import json
import secrets
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from model_trial import load_key  # noqa: E402

from faultforge import fix_agent  # noqa: E402
from faultforge.fix_agent import ModelPrice, TokenBudget  # noqa: E402
from faultforge.stack import StackSpec, load_stack, run_stack  # noqa: E402
from faultforge.stack_fix import load_fix_config, read_image_file, redact, run_stack_fix_loop  # noqa: E402
from faultforge.web import viewmodel  # noqa: E402
from faultforge.web.targets import sportsdeck_replay_target  # noqa: E402

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
    parser.add_argument("--record", type=Path, help="write each try as a replay in this directory")
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
    target = sportsdeck_replay_target()
    passed = 0
    for number in range(1, args.tries + 1):
        project = args.output / f"try-{number}"
        baseline = run_stack(StackSpec(config.base_image, stack, CONFIG), project, SCENARIO)
        if baseline.status != "completed" or baseline.report.verdict != "FAIL":
            print(f"try {number}: the baseline wasn't FAIL: {baseline.status} {baseline.failure_reason}")
            continue

        attempts_view = []
        latest = {}

        def sandboxed(spec, project_dir, scenario_id, options):
            result = run_stack(spec, project_dir, scenario_id, options)
            # The page's evidence is the main scenario. Regression runs are reported on the attempt.
            if scenario_id == SCENARIO:
                latest["result"] = result
            return result

        def on_attempt(record, proposal, built, try_number=number, project=project) -> None:
            main_result = latest.pop("result", None)
            candidate = viewmodel.build_run_view(main_result, target, project) if main_result is not None else None
            attempts_view.append(viewmodel.build_attempt_view(record, proposal, sources[config.editable].encode(), candidate))
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
            attempts=args.attempts, reasoning_effort=EFFORT, on_attempt=on_attempt, run_sandboxed=sandboxed,
        )
        if args.record:
            stop = result.stop_reason
            replay = viewmodel.ReplayView(
                id=f"r_{secrets.token_hex(8)}", recorded_at=viewmodel.timestamp(time.time()), target="sportsdeck",
                recorded_on="dev_machine", result=viewmodel.build_run_view(baseline, target, project),
                fix=viewmodel.FixView(
                    status="verified" if result.success else "not_verified", model=MODEL, reasoning_effort=EFFORT,
                    max_attempts=args.attempts, attempts=attempts_view,
                    stop_reason=stop if stop in viewmodel.STOP_TEXT else "other",
                    stop_text=viewmodel.STOP_TEXT.get(stop, "The fix stopped."),
                    total_cost_usd=round(sum(item.cost_usd for item in attempts_view), 6),
                ),
            )
            args.record.mkdir(parents=True, exist_ok=True)
            (args.record / f"{replay.id}.json").write_text(json.dumps(replay.model_dump(mode="json")), encoding="utf-8")
        passed += result.success
        print(f"try {number}: success={result.success} stop={result.stop_reason} attempts={len(result.attempts)}", flush=True)
    print(f"verified {passed} of {args.tries}; spent ${budget.spent_usd:.4f} of ${args.budget_usd:.2f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
