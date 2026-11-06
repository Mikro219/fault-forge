"""Measures how often each Vultr inference model fixes the ticket-api bug, within a USD cap.

Gets a baseline FAIL in a sandbox, then runs one-attempt candidates model by model, round robin, so
an expensive model can't spend the cap before a cheap one has had a try. With --attempts above 1
it also runs the full retry loop and records that in a separate file. --dry-run uses a fake model
and a fake sandbox: it needs no key, no network and no Docker, and its numbers mean nothing.
"""

import argparse
import hashlib
import json
import math
import statistics
import sys
import time
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import httpx

from faultforge import fix_agent
from faultforge.fix_agent import ApiKey, AttemptRecord, LoopResult, ModelPrice, Proposal, TokenBudget
from faultforge.lifecycle import RunOptions
from faultforge.models import (
    Budget,
    Build,
    ClientCounts,
    Comparison,
    Completeness,
    Engine,
    Hypothesis,
    InvariantComparison,
    InvariantResult,
    LoadConfig,
    Report,
    RunConfig,
    TargetInfo,
    TrialResult,
    Violation,
)
from faultforge.sandbox import (
    TICKET_API_STATE_VERIFIER,
    ContainmentFacts,
    SandboxResult,
    StateVerification,
    TargetSpec,
    run_sandboxed,
)

ROOT = Path(__file__).resolve().parents[1]
TICKET_API = ROOT / "demo" / "ticket-api"
SCENARIO = "last-ticket-rush"
KEY_NAME = "VULTR_INFERENCE_API_KEY"
DEFAULT_OUTPUT_DIR = Path(
    "/tmp/claude-1000/-home-mikep-projects-fault-forge/2e457d6d-515e-4b0c-98cc-526302806750/scratchpad"
)
DEFAULT_MAX_TOKENS = 6000
DRY_RUN_KEY = "dry-run-not-a-key"
DRY_RUN_CODE = """\
from ticket_support import payments
from ticket_support.server import ReserveRequest, create_app

app = create_app()


@app.post("/reserve")
async def reserve(body: ReserveRequest):
    return {"order_id": (await payments.authorize(body.buyer, key="dry-run")).id}
"""

# USD per 1M tokens, from https://api.vultrinference.com/docs/models
PRICES = {
    "glm-5.3": ModelPrice(0.75, 3.00),
    "mimo-v2.6-pro": ModelPrice(0.40, 0.80),
    "qwen3.8-27b": ModelPrice(0.15, 1.00),
    "deepseek-v4.1-flash-1017": ModelPrice(0.15, 0.60),
    "glm-5.3-flash": ModelPrice(0.10, 0.35),
    "mimo-v2.6-flash": ModelPrice(0.10, 0.25),
    "qwen3.8-flash-next": ModelPrice(0.10, 0.20),
}


def split_configuration(configuration: str) -> tuple[str, str | None]:
    """model@effort gives (model, effort). A bare model gives (model, None): no setting is sent."""
    model, separator, effort = configuration.partition("@")
    return model, effort if separator else None


def configuration_name(model: str, reasoning_effort: str | None) -> str:
    return model if reasoning_effort is None else f"{model}@{reasoning_effort}"


class KeyFileError(Exception):
    """Never quotes the file: any line of it could be the key."""


def load_key(path: Path) -> ApiKey:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as error:
        raise KeyFileError(f"can't read the key file: {type(error).__name__}") from None
    for line in text.splitlines():
        name, separator, value = line.strip().removeprefix("export ").partition("=")
        if separator and name.strip() == KEY_NAME:
            try:
                return ApiKey(value.strip().strip("'\""))
            except ValueError:
                raise KeyFileError(f"{KEY_NAME} in the key file is malformed") from None
    raise KeyFileError(f"the key file has no {KEY_NAME} line")


# Synthetic data for --dry-run and the tests


def synthetic_report(
    run_id: str, verdict: str = "FAIL", comparison: Comparison | None = None
) -> Report:
    """A report that satisfies every validator. It describes no real run."""
    failed = verdict == "FAIL"
    trial = TrialResult(
        trial=1, kind="concurrent", status="completed", planned_requests=20, completed_requests=20,
        peak_in_flight=20,
        client=ClientCounts(accepted=20 if failed else 1, rejected=0 if failed else 19, unexpected=0, transport_error=0),
        invariants=[
            InvariantResult(
                name="No more tickets confirmed than were in stock", result="fail" if failed else "pass",
                values={"client.accepted": 20 if failed else 1, "before.stock": 1},
            )
        ],
    )
    violations = [
        Violation(
            id="V1", type="invariant.capacity_exceeded", invariant="No more tickets confirmed than were in stock",
            trials_violated="1/1", observed={"client.accepted": 20, "before.stock": 1},
            facts={"accepted_overlap": True, "overlap_window_ms": 12.5, "status_200": 20},
        )
    ] if failed else []
    hypotheses = [
        Hypothesis(
            id="H1", suspected_cause="check_then_act_race", status="suspected", explains=["V1"],
            basis=["capacity_exceeded", "accepted_overlap"], how_to_confirm="Synthetic.",
        )
    ] if failed else []
    return Report(
        run_id=run_id, scenario_id=SCENARIO,
        completeness=Completeness(
            status="complete", trials_planned=1, trials_completed=1, trials_incomplete=0,
            trials_skipped=0, serial_control="not_requested",
        ),
        budget=Budget(max_requests_total=100, requests_planned=20, requests_sent=20, max_wall_s_total=60, wall_s_used=1),
        target=TargetInfo(
            origin="http://127.0.0.1:8000",
            build=Build(build_id="synthetic", instance_id="synthetic", instance_started_at="synthetic", stale_source=False),
            build_stable_during_run=True,
        ),
        config=RunConfig(
            load=LoadConfig(vus=20, iterations=20, max_duration_s=10), trials=1, serial_control=False,
            config_hash="sha256:" + "0" * 64, engine=Engine(name="k6", version="synthetic"),
            mutates_data=True, user_confirmed=True,
        ),
        trials=[trial], verdict=verdict, verdict_reason="Synthetic.", violations=violations,
        hypotheses=hypotheses, comparison=comparison,
    )


def _synthetic_sandbox_result(report: Report, spec: TargetSpec) -> SandboxResult:
    return SandboxResult(
        status="completed", report=report, deployed_code_sha256=hashlib.sha256(spec.app_source).hexdigest(),
        containment=ContainmentFacts(wall_s=0, wall_limit_s=100, state_verification=StateVerification(status="matched")),
    )


class DryRunSandbox:
    """Fails the run with no compare_to, which is the baseline, and verifies every candidate."""

    def __init__(self) -> None:
        self.runs = 0

    def __call__(self, spec: TargetSpec, project_dir: Path, scenario_id: str, options: RunOptions) -> SandboxResult:
        self.runs += 1
        run_id = f"run_20260101_000000_{self.runs:04x}"
        if options.compare_to is None:
            return _synthetic_sandbox_result(synthetic_report(run_id), spec)
        comparison = Comparison(
            baseline_run_id=options.compare_to, status="COMPARABLE", fix_verified=True,
            invariants=[InvariantComparison(name="No more tickets confirmed than were in stock", result="FIXED")],
        )
        return _synthetic_sandbox_result(synthetic_report(run_id, "PASS", comparison), spec)


def dry_run_transport() -> httpx.MockTransport:
    def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "choices": [{"finish_reason": "stop", "message": {"content": f"```python\n{DRY_RUN_CODE}```"}}],
            "usage": {"prompt_tokens": 5000, "completion_tokens": 500},
        })

    return httpx.MockTransport(respond)


# Output


@dataclass
class Output:
    """Every line is flushed when written, so a stopped run keeps what it paid for."""

    tries_path: Path
    loops_path: Path
    code_dir: Path
    project_dir: Path
    dry_run: bool
    secret: str | None = None
    code_files: dict[str, str] = field(default_factory=dict)

    @classmethod
    def create(cls, tries_path: Path, dry_run: bool, secret: str | None) -> "Output":
        stem = tries_path.with_suffix("")
        output = cls(
            tries_path=tries_path, loops_path=stem.with_name(f"{stem.name}.loops.jsonl"),
            code_dir=stem.with_name(f"{stem.name}.code"), project_dir=stem.with_name(f"{stem.name}.project"),
            dry_run=dry_run, secret=secret,
        )
        for path in (output.tries_path, output.loops_path, output.code_dir, output.project_dir):
            if path.exists():
                raise FileExistsError(f"{path} already exists; choose a new --output")
        tries_path.parent.mkdir(parents=True, exist_ok=True)
        # Only FaultForge's files go here, because it's mounted in the runner.
        output.project_dir.mkdir()
        output.code_dir.mkdir()
        return output

    def append(self, path: Path, record: dict[str, object]) -> None:
        line = json.dumps({**record, "dry_run": self.dry_run})
        if self.secret:
            line = line.replace(self.secret, "[REDACTED]")
        with path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")

    def save_code(self, label: str, proposal: Proposal) -> str | None:
        text, suffix = (proposal.code, ".py.txt") if proposal.code is not None else (proposal.raw_output, ".rejected.txt")
        if text is None:
            return None
        # The effort is in the name, or two configurations of one model would share files.
        path = self.code_dir / f"{configuration_name(proposal.model, proposal.reasoning_effort)}-{label}{suffix}"
        path.write_text(text, encoding="utf-8", errors="replace")
        return str(path)


# The trial


@dataclass(frozen=True)
class TrialInputs:
    app_source: bytes
    support_sources: dict[str, bytes]
    business_rules: bytes
    support_dir: Path
    config_path: Path


def load_inputs(project: Path = TICKET_API) -> TrialInputs:
    support_dir = project / "ticket_support"
    return TrialInputs(
        app_source=fix_agent.read_prompt_file(project / "app.py"),
        support_sources=fix_agent.load_support_sources(support_dir),
        business_rules=fix_agent.read_prompt_file(project / "BUSINESS_RULES.md"),
        support_dir=support_dir,
        config_path=project / "faultforge.yaml",
    )


@dataclass
class TrialSettings:
    models: list[str]
    tries_per_model: int
    attempts: int
    loops_per_model: int
    max_tokens: int


def _run_model(
    configuration: str, attempts: int, label: str, on_record: Callable[[AttemptRecord, str | None], None],
    inputs: TrialInputs, settings: TrialSettings, baseline: Report, baseline_code_sha256: str | None,
    api_key: ApiKey, budget: TokenBudget, output: Output, sandbox: Callable[..., SandboxResult], transport: httpx.BaseTransport | None,
    sleep: Callable[[float], None],
) -> LoopResult:
    def on_attempt(record: AttemptRecord, proposal: Proposal) -> None:
        on_record(record, output.save_code(f"{label}-attempt-{record.attempt}", proposal))

    model, reasoning_effort = split_configuration(configuration)
    return fix_agent.run_fix_loop(
        model=model, api_key=api_key, budget=budget, app_source=inputs.app_source,
        support_sources=inputs.support_sources, business_rules=inputs.business_rules, baseline=baseline,
        baseline_code_sha256=baseline_code_sha256,
        support_dir=inputs.support_dir, config_path=inputs.config_path, project_dir=output.project_dir,
        scenario_id=SCENARIO, run_sandboxed=sandbox, options=RunOptions(), attempts=attempts,
        max_tokens=settings.max_tokens, reasoning_effort=reasoning_effort, transport=transport, sleep=sleep, on_attempt=on_attempt,
    )


def run_trial(
    settings: TrialSettings, inputs: TrialInputs, api_key: ApiKey, budget: TokenBudget, output: Output,
    sandbox: Callable[..., SandboxResult], transport: httpx.BaseTransport | None = None,
    sleep: Callable[[float], None] = time.sleep, print_line: Callable[[str], None] = print,
) -> tuple[list[dict[str, object]], str]:
    """Returns the try records and why the trial stopped. No model is called without a baseline FAIL."""
    spec = TargetSpec(
        app_source=inputs.app_source, support_dir=inputs.support_dir, config_path=inputs.config_path,
        state_verifier=TICKET_API_STATE_VERIFIER,
    )
    result = sandbox(spec, output.project_dir, SCENARIO, RunOptions())
    report = result.report
    facts = result.containment.state_verification
    output.append(output.tries_path, {
        "record": "baseline", "status": result.status, "failure_reason": result.failure_reason,
        "run_id": report.run_id if report else None, "verdict": report.verdict if report else None,
        "violation_types": [violation.type for violation in report.violations] if report else [],
        "deployed_code_sha256": result.deployed_code_sha256,
        "state_verification": facts.status if facts else None,
    })
    if report is None or report.verdict != "FAIL":
        verdict = report.verdict if report else None
        print_line(f"no baseline FAIL (status {result.status}, reason {result.failure_reason}, verdict {verdict}); no model was called")
        return [], "baseline_not_fail"

    records: list[dict[str, object]] = []
    active = list(settings.models)
    stop = "completed"
    shared = (inputs, settings, report, result.deployed_code_sha256, api_key, budget, output, sandbox, transport, sleep)

    for try_number in range(1, settings.tries_per_model + 1):
        for configuration in list(active):
            def on_record(record: AttemptRecord, code_file: str | None) -> None:
                line = {
                    "record": "try", "try": try_number, "configuration": configuration, "code_file": code_file,
                    "deployed_code_sha256_present": record.deployed_code_sha256 is not None, **record.to_dict(),
                }
                output.append(output.tries_path, line)
                records.append(line)

            loop = _run_model(configuration, 1, f"try-{try_number:02d}", on_record, *shared)
            if loop.stop_reason == "budget_exhausted":
                active.remove(configuration)
                print_line(f"{configuration}: stopped by the budget after {try_number - 1} tries")
            elif loop.stop_reason == "auth_rejected":
                print_line("the provider rejected the key; stopping")
                return records, "auth_rejected"
        if not active:
            return records, "budget_exhausted"
    if len(active) < len(settings.models):
        stop = "budget_exhausted"

    if settings.attempts > 1:
        for configuration in active:
            for loop_number in range(1, settings.loops_per_model + 1):
                loop = _run_model(configuration, settings.attempts, f"loop-{loop_number:02d}", lambda *_: None, *shared)
                output.append(
                    output.loops_path, {"record": "loop", "loop": loop_number, "configuration": configuration, **loop.to_dict()}
                )
                print_line(
                    f"{configuration}: loop {loop_number}: {loop.stop_reason} after {len(loop.attempts)} attempts, "
                    f"${loop.cost_usd:.4f}"
                )
                if loop.stop_reason in ("budget_exhausted", "auth_rejected"):
                    return records, loop.stop_reason
    return records, stop


def _configuration_of(record: dict[str, object]) -> str:
    return configuration_name(str(record["model"]), record.get("reasoning_effort"))


def _was_run(record: dict[str, object]) -> bool:
    """Valid code that the policy let through, so it went to a sandbox."""
    return bool(record["syntactically_valid"]) and not record.get("policy_violations")


def summarize(records: list[dict[str, object]], configurations: list[str]) -> dict[str, dict[str, object]]:
    """Grouped by model@effort. A try the budget refused sent nothing, so it isn't counted as a try.
    fix_verified is the full gate, as it is on a try record; fix_verified_alone is the comparison's
    answer without the state and code checks. The verdict and state counts cover tries that ran."""
    summary = {}
    for configuration in configurations:
        tries = [
            r for r in records
            if _configuration_of(r) == configuration and r["failure_reason"] != "budget_exhausted"
        ]
        ran = [r for r in tries if _was_run(r)]
        summary[configuration] = {
            "tries": len(tries),
            "syntactically_valid": sum(bool(r["syntactically_valid"]) for r in tries),
            "policy_rejected": sum(r["failure_reason"] == "policy_violation" for r in tries),
            "truncated": sum(r["failure_reason"] == "output_truncated" for r in tries),
            "valid_and_run": len(ran),
            "verdicts": dict(Counter(str(r.get("verdict")) for r in ran)),
            "state_verification": dict(Counter(str(r.get("state_verification")) for r in ran)),
            "fix_verified_alone": sum(bool(r.get("comparison_fix_verified")) for r in tries),
            "full_gate_success": sum(bool(r["fix_verified"]) for r in tries),
            "fix_verified": sum(bool(r["fix_verified"]) for r in tries),
            "failure_types": dict(Counter(str(r["failure_reason"]) for r in tries if r["failure_reason"])),
            "input_tokens": sum(int(r["input_tokens"]) for r in tries),
            "output_tokens": sum(int(r["output_tokens"]) for r in tries),
            "reasoning_tokens": sum(int(r.get("reasoning_tokens") or 0) for r in tries),
            "cost_usd": round(sum(float(r["cost_usd"]) for r in tries), 6),
            "median_model_wall_s": round(statistics.median(r["model_wall_seconds"] for r in tries), 2) if tries else None,
            "median_total_wall_s": round(statistics.median(r["wall_seconds"] for r in tries), 2) if tries else None,
        }
    return summary


def print_summary(summary: dict[str, dict[str, object]], print_line: Callable[[str], None] = print) -> None:
    for configuration, row in summary.items():
        print_line(
            f"{configuration}: tries={row['tries']} policy_rejected={row['policy_rejected']} "
            f"truncated={row['truncated']} valid_and_run={row['valid_and_run']} verdicts={row['verdicts']} "
            f"state_verification={row['state_verification']} fix_verified_alone={row['fix_verified_alone']} "
            f"full_gate_success={row['full_gate_success']} "
            f"median_wall_s(model/total)={row['median_model_wall_s']}/{row['median_total_wall_s']} "
            f"tokens={row['input_tokens']}+{row['output_tokens']} (reasoning {row['reasoning_tokens']}) "
            f"cost=${row['cost_usd']:.4f} failures={row['failure_types']}"
        )


# Command line


def _positive_usd(text: str) -> float:
    value = float(text)
    if not math.isfinite(value) or value <= 0:
        raise argparse.ArgumentTypeError("must be a positive number")
    return value


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--models", required=True, help="comma-separated model or model@effort; efforts: " + ", ".join(fix_agent.REASONING_EFFORTS) + "; known: " + ", ".join(PRICES))
    parser.add_argument("--tries-per-model", type=int, default=1)
    parser.add_argument("--budget-usd", type=_positive_usd, required=True)
    parser.add_argument("--key-file", type=Path, help=f"holds {KEY_NAME}=...; not needed with --dry-run")
    parser.add_argument("--output", type=Path, help="a JSONL path outside the repo; default: the session scratchpad")
    parser.add_argument("--dry-run", action="store_true", help="fake model and fake sandbox; numbers mean nothing")
    parser.add_argument("--attempts", type=int, default=1, choices=range(1, fix_agent.MAX_ATTEMPTS + 1), help="above 1 also runs the retry loop")
    parser.add_argument("--loops-per-model", type=int, default=1, help="retry loops per model when --attempts is above 1")
    parser.add_argument("--max-tokens", type=int, default=DEFAULT_MAX_TOKENS)
    return parser


def _models(text: str) -> list[str]:
    configurations = [item.strip() for item in text.split(",") if item.strip()]
    split = [split_configuration(item) for item in configurations]
    unknown = [model for model, _ in split if model not in PRICES]
    if unknown:
        raise ValueError(f"unknown model, with no known price: {', '.join(unknown)}")
    if any(effort is not None and effort not in fix_agent.REASONING_EFFORTS for _, effort in split):
        raise ValueError(f"the effort after @ must be one of {', '.join(fix_agent.REASONING_EFFORTS)}")
    if not configurations or len(configurations) != len(set(configurations)):
        raise ValueError("--models must list at least one model, with no repeats")
    return configurations


def _output_path(requested: Path | None) -> Path:
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    path = (requested or DEFAULT_OUTPUT_DIR / f"model-trial-{stamp}.jsonl").resolve()
    if path.is_relative_to(ROOT):
        raise ValueError("--output must be outside the repo")
    return path


def _worst_case_line(settings: TrialSettings, inputs: TrialInputs, budget: TokenBudget) -> str:
    baseline = synthetic_report("run_20260101_000000_0000")
    messages = fix_agent.build_messages(
        inputs.app_source, inputs.support_sources, inputs.business_rules, baseline,
        invariant_names=fix_agent.invariant_names_for(inputs.config_path, SCENARIO),
    )
    tokens = fix_agent.estimate_input_tokens(messages)
    # One cost per configuration: two efforts of one model are two sets of tries, at the model's price.
    per_try = [budget.cost(split_configuration(item)[0], tokens, settings.max_tokens) for item in settings.models]
    total = sum(per_try) * settings.tries_per_model
    return f"worst case for the tries: ${total:.2f} against a ${budget.cap_usd:.2f} cap (first prompt about {tokens} tokens at most)"


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        models = _models(args.models)
        output_path = _output_path(args.output)
    except ValueError as error:
        parser.error(str(error))
    if args.tries_per_model < 1 or args.loops_per_model < 1 or args.max_tokens < 1:
        parser.error("--tries-per-model, --loops-per-model and --max-tokens must be at least 1")
    if not args.dry_run and args.key_file is None:
        parser.error("--key-file is required unless --dry-run is given")

    try:
        api_key = ApiKey(DRY_RUN_KEY) if args.dry_run else load_key(args.key_file)
    except KeyFileError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2

    settings = TrialSettings(models, args.tries_per_model, args.attempts, args.loops_per_model, args.max_tokens)
    budget = TokenBudget(cap_usd=args.budget_usd, prices=PRICES)
    inputs = load_inputs()
    try:
        output = Output.create(output_path, args.dry_run, None if args.dry_run else api_key.reveal())
    except (FileExistsError, OSError) as error:
        print(f"error: can't create the output: {error}", file=sys.stderr)
        return 2
    sandbox, transport = (DryRunSandbox(), dry_run_transport()) if args.dry_run else (run_sandboxed, None)

    if args.dry_run:
        print("DRY RUN: no model called, no sandbox started; the results below are synthetic")
    print(_worst_case_line(settings, inputs, budget))
    records, stop = run_trial(settings, inputs, api_key, budget, output, sandbox, transport)
    summary = summarize(records, models)
    output.append(output.tries_path, {
        "record": "summary", "stop": stop, "spent_usd": round(budget.spent_usd, 6),
        "cap_usd": budget.cap_usd, "models": summary,
    })
    print_summary(summary)
    print(f"stop: {stop}; spent ${budget.spent_usd:.4f} of ${budget.cap_usd:.2f}")
    print(f"tries: {output.tries_path}\nloops: {output.loops_path}\ncode: {output.code_dir}")
    return 1 if stop == "baseline_not_fail" or stop == "auth_rejected" else 0


if __name__ == "__main__":
    sys.exit(main())
