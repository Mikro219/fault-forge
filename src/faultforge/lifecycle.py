"""Runs a scenario: admission, then reset, load, settle and snapshot for each trial, then the verdict."""

import hashlib
import json
import subprocess
import time
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

from pydantic import ValidationError

from faultforge import config as config_module
from faultforge import expressions, k6_runner, oracle, report as report_module, safety
from faultforge.errors import FaultForgeError
from faultforge.evidence import count_outcomes
from faultforge.hooks import HookError, Snapshot, TargetHooks
from faultforge.k6_runner import EngineResult, K6Missing, TrialSpec
from faultforge.limits import LIMITS, Limits
from faultforge.models import (
    Budget,
    Build,
    Clamp,
    ClientCounts,
    Comparison,
    Completeness,
    Engine,
    Evidence,
    IncompleteReason,
    LoadConfig,
    ProjectConfig,
    Report,
    RequestRecord,
    Reproduce,
    RunConfig,
    ScenarioConfig,
    TargetInfo,
    TrialResult,
)

MIN_TRIAL_TIME_S = 1
SETTLE_POLL_S = 0.05
EMPTY_COUNTS = ClientCounts(accepted=0, rejected=0, unexpected=0, transport_error=0)


@dataclass(frozen=True)
class RunOptions:
    trials: int | None = None
    serial_control: bool | None = None
    vus: int | None = None
    iterations: int | None = None
    max_duration_s: int | None = None
    compare_to: str | None = None


DEFAULT_TRIALS = 3
DEFAULT_SERIAL_CONTROL = True


class NotComparable(Exception):
    """A retest doesn't match its baseline, so it was refused without sending load."""

    def __init__(self, comparison: Comparison):
        super().__init__(comparison.not_comparable_reason)
        self.comparison = comparison


@dataclass(frozen=True)
class Plan:
    load: LoadConfig
    trials: int
    serial_control: bool
    clamps: tuple[Clamp, ...]

    @property
    def trial_count(self) -> int:
        return self.trials + (1 if self.serial_control else 0)

    @property
    def planned_requests(self) -> int:
        return self.trial_count * self.load.iterations


def resolve_load(
    base: LoadConfig, options: RunOptions, limits: Limits
) -> tuple[LoadConfig, tuple[Clamp, ...]]:
    """An override of vus discards the configured min_peak_in_flight, so the default still
    requires real overlap. vus=1 therefore can't pass as a concurrent trial."""
    requested = {
        "vus": base.vus if options.vus is None else options.vus,
        "iterations": base.iterations if options.iterations is None else options.iterations,
        "max_duration_s": base.max_duration_s if options.max_duration_s is None else options.max_duration_s,
    }
    caps = {
        "vus": limits.max_vus,
        "iterations": limits.max_iterations_per_trial,
        "max_duration_s": limits.max_trial_duration_s,
    }
    clamps = []
    effective = {}
    for field, value in requested.items():
        if value < 1:
            raise FaultForgeError("config_invalid", f"{field} must be at least 1")
        effective[field] = min(value, caps[field])
        if value > caps[field]:
            clamps.append(Clamp(field=field, requested=value, effective=caps[field]))
    if effective["iterations"] < effective["vus"]:
        raise FaultForgeError("config_invalid", "iterations must be at least vus")
    min_peak = base.min_peak_in_flight if effective["vus"] == base.vus else None
    if min_peak is not None and min_peak > effective["vus"]:
        raise FaultForgeError("config_invalid", "min_peak_in_flight can't exceed vus")
    return LoadConfig(**effective, min_peak_in_flight=min_peak), tuple(clamps)


def resolve_plan(
    scenario: ScenarioConfig, options: RunOptions, baseline: Report | None, limits: Limits
) -> Plan:
    if baseline is not None:
        return _inherit_plan(options, baseline, limits)
    load, clamps = resolve_load(scenario.load, options, limits)
    trials = DEFAULT_TRIALS if options.trials is None else options.trials
    serial = DEFAULT_SERIAL_CONTROL if options.serial_control is None else options.serial_control
    return Plan(load=load, trials=trials, serial_control=serial, clamps=clamps)


def _inherit_plan(options: RunOptions, baseline: Report, limits: Limits) -> Plan:
    """A retest runs what the baseline ran. A different explicit value is refused, not applied."""
    config = baseline.config
    load, clamps = resolve_load(
        config.load,
        RunOptions(vus=options.vus, iterations=options.iterations, max_duration_s=options.max_duration_s),
        limits,
    )
    expected = (config.load.vus, config.load.iterations, config.load.max_duration_s)
    if (load.vus, load.iterations, load.max_duration_s) != expected:
        raise NotComparable(report_module.not_comparable(baseline.run_id, "load differs from the baseline"))
    if options.trials is not None and options.trials != config.trials:
        raise NotComparable(report_module.not_comparable(baseline.run_id, "trials differs from the baseline"))
    if options.serial_control is not None and options.serial_control != config.serial_control:
        raise NotComparable(report_module.not_comparable(baseline.run_id, "serial_control differs from the baseline"))
    return Plan(
        load=config.load, trials=config.trials, serial_control=config.serial_control, clamps=clamps
    )


def admit(plan: Plan, limits: Limits) -> None:
    """Rejects an over-budget plan. Trials are never silently reduced."""
    budget_wall = limits.max_wall_s_total - limits.admission_margin_s
    per_trial_s = plan.load.max_duration_s + limits.trial_overhead_s
    wall_s = plan.trial_count * per_trial_s
    fitting = int(budget_wall // per_trial_s)
    suggested_trials = max(1, min(fitting, limits.max_trials) - (1 if plan.serial_control else 0))
    suggested = {
        "trials": suggested_trials,
        "iterations": min(plan.load.iterations, limits.max_requests_total // (suggested_trials + (1 if plan.serial_control else 0))),
        "max_duration_s": plan.load.max_duration_s,
    }
    if plan.trials < 1:
        raise FaultForgeError("config_invalid", "trials must be at least 1")
    reasons = []
    if plan.trial_count > limits.max_trials:
        reasons.append(f"{plan.trial_count} trials exceeds the limit of {limits.max_trials}")
    if plan.planned_requests > limits.max_requests_total:
        reasons.append(f"{plan.planned_requests} requests exceeds the limit of {limits.max_requests_total}")
    if wall_s > budget_wall:
        reasons.append(f"up to {wall_s:g} s exceeds the {budget_wall:g} s wall budget")
    if reasons:
        raise FaultForgeError(
            "budget_exceeded", "; ".join(reasons), "Reduce the plan.", suggested_plan=suggested
        )


def config_hash(
    project: ProjectConfig, scenario: ScenarioConfig, load: LoadConfig, k6_version: str
) -> str:
    """Covers what defines the workload, including the effective load. The port doesn't count."""
    target = project.target
    canonical = {
        "load": {
            "vus": load.vus,
            "iterations": load.iterations,
            "max_duration_s": load.max_duration_s,
            "min_peak_in_flight": load.effective_min_peak_in_flight,
        },
        "request": scenario.request.model_dump(by_alias=True),
        "outcomes": scenario.outcomes.model_dump(),
        "invariants": [invariant.model_dump() for invariant in scenario.invariants],
        "target": {
            "health": target.health,
            "state": target.state,
            "reset": target.reset.model_dump(by_alias=True) if target.reset else None,
        },
        "script": k6_runner.script_sha256(scenario.script),
        "k6_version": k6_version,
    }
    encoded = json.dumps(canonical, sort_keys=True, separators=(",", ":"))
    return f"sha256:{hashlib.sha256(encoded.encode()).hexdigest()}"


def _preflight(hooks: TargetHooks, scenario: ScenarioConfig, origin: str) -> Snapshot:
    """Read-only: nothing is reset and no load is sent."""
    try:
        hooks.health()
        snapshot = hooks.state()
    except HookError as error:
        raise FaultForgeError(
            "target_unreachable",
            f"{origin} isn't ready: {error}",
            f"Start the target on {origin}, with its test hooks enabled.",
        ) from error
    for invariant in scenario.invariants:
        for variable in expressions.parse(invariant.check).variables:
            namespace, _, field = variable.partition(".")
            if namespace != "client" and field not in snapshot.values:
                raise FaultForgeError(
                    "config_invalid",
                    f"invariant {invariant.name!r} uses {variable}, which the state hook doesn't report",
                )
    return snapshot


def _settle(hooks: TargetHooks, limits: Limits) -> Snapshot:
    """Waits for in-flight work to finish, so the after-snapshot isn't mid-transition."""
    deadline = time.monotonic() + limits.settle_cap_s
    while True:
        snapshot = hooks.state()
        if not snapshot.in_flight:
            return snapshot
        if time.monotonic() >= deadline:
            raise HookError("target didn't settle")
        time.sleep(SETTLE_POLL_S)


def _wait_until_idle(hooks: TargetHooks, limits: Limits) -> None:
    """Stragglers from a timed-out trial must end before reset, or they'd land in the next trial.
    A failure here isn't final: the before-snapshot decides whether the trial can run."""
    try:
        _settle(hooks, limits)
    except HookError:
        pass


def _reset_took_effect(project: ProjectConfig, before: Snapshot) -> bool:
    reset = project.target.reset
    if reset is None:
        return True
    return all(
        before.values.get(key) == value
        for key, value in reset.json_body.items()
        if type(value) is int
    )


def _skipped(number: int, kind: str, planned: int) -> TrialResult:
    return TrialResult(
        trial=number, kind=kind, status="skipped", incomplete_reason="budget_exhausted",
        planned_requests=planned, completed_requests=0, client=EMPTY_COUNTS,
    )


def _stopped_early(
    number: int, kind: str, planned: int, reason: IncompleteReason, before: Snapshot | None
) -> TrialResult:
    return TrialResult(
        trial=number, kind=kind, status="incomplete", incomplete_reason=reason,
        planned_requests=planned, completed_requests=0, client=EMPTY_COUNTS,
        state_before=before.values if before else None,
    )


@dataclass
class TrialOutcome:
    result: TrialResult
    engine: EngineResult | None
    builds: tuple[Build, ...]
    restart_undetectable: bool = False


def _run_one_trial(
    number: int,
    kind: str,
    load: LoadConfig,
    project: ProjectConfig,
    scenario: ScenarioConfig,
    hooks: TargetHooks,
    k6: Path,
    run_directory: Path,
    deadline: float,
    limits: Limits,
) -> TrialOutcome:
    planned = load.iterations
    trial_dir = run_directory / "trials" / str(number)
    trial_dir.mkdir(parents=True, exist_ok=True)

    _wait_until_idle(hooks, limits)
    try:
        hooks.reset()
    except HookError:
        return TrialOutcome(_stopped_early(number, kind, planned, "reset_failed", None), None, ())
    try:
        before = hooks.state()
    except HookError:
        return TrialOutcome(_stopped_early(number, kind, planned, "state_snapshot_failed", None), None, ())
    report_module.write_json(trial_dir / "state-before.json", before.raw)
    builds = (before.build,) if before.build else ()
    if before.in_flight or not _reset_took_effect(project, before):
        return TrialOutcome(_stopped_early(number, kind, planned, "reset_failed", before), None, builds)

    # Measured after the hooks, which can each use their full timeout. Reserving time for the
    # kill and for settling keeps a stuck trial within the run's wall budget.
    remaining_s = deadline - time.monotonic() - limits.trial_overhead_s - k6_runner.KILL_GRACE_S
    if remaining_s < MIN_TRIAL_TIME_S:
        return TrialOutcome(_skipped(number, kind, planned), None, builds)
    max_duration = min(load.max_duration_s, int(remaining_s))
    bounded = LoadConfig(
        vus=load.vus, iterations=load.iterations, max_duration_s=max_duration,
        min_peak_in_flight=load.min_peak_in_flight,
    )
    spec = TrialSpec(
        origin=project.target.origin, request=scenario.request, outcomes=scenario.outcomes,
        load=bounded, trial=number, kind=kind, script=scenario.script,
    )
    engine = k6_runner.run_trial(spec, trial_dir, run_directory / report_module.REQUESTS_NAME, k6=k6)

    after: Snapshot | None = None
    snapshot_failed = False
    try:
        after = _settle(hooks, limits)
        report_module.write_json(trial_dir / "state-after.json", after.raw)
    except HookError:
        snapshot_failed = True
    if after is not None and after.build:
        builds = (*builds, after.build)

    instance_before = before.build.instance_id if before.build else None
    instance_after = after.build.instance_id if after and after.build else None
    restarted = after is not None and instance_before != instance_after
    reason: IncompleteReason | None
    if restarted:
        reason = "target_restarted"
    elif engine.incomplete_reason is not None:
        reason = engine.incomplete_reason
    elif snapshot_failed:
        reason = "state_snapshot_failed"
    else:
        reason = None

    client = count_outcomes(engine.records)
    completed = reason is None
    return TrialOutcome(
        TrialResult(
            trial=number, kind=kind, status="completed" if completed else "incomplete",
            incomplete_reason=reason, planned_requests=planned,
            completed_requests=len(engine.records), peak_in_flight=engine.peak_in_flight,
            client=client, state_before=before.values,
            state_after=after.values if after else None,
            invariants=oracle.evaluate_invariants(
                scenario.invariants, client, before.values, after.values if after else None, completed
            ),
        ),
        engine,
        builds,
        restart_undetectable=after is not None and instance_before is None and instance_after is None,
    )


def _completeness(trials: Sequence[TrialResult], planned: int, serial_control: bool) -> Completeness:
    concurrent = [trial for trial in trials if trial.kind == "concurrent"]
    control = next((trial for trial in trials if trial.kind == "serial_control"), None)
    done = sum(trial.status == "completed" for trial in concurrent)
    incomplete = sum(trial.status == "incomplete" for trial in concurrent)
    skipped = sum(trial.status == "skipped" for trial in concurrent)
    control_status = "not_requested" if not serial_control else (control.status if control else "skipped")
    return Completeness(
        status="complete" if done == planned and control_status in ("completed", "not_requested") else "incomplete",
        trials_planned=planned,
        trials_completed=done,
        trials_incomplete=incomplete,
        trials_skipped=skipped,
        serial_control=control_status,
    )


def _summarize_build(builds: Sequence[Build]) -> tuple[Build, bool | None]:
    if not builds:
        unknown = Build(build_id=None, instance_id=None, instance_started_at=None, stale_source=None)
        return unknown, None
    first = builds[0]
    stable = all(
        build.build_id == first.build_id and build.instance_id == first.instance_id for build in builds
    )
    stale_values = [build.stale_source for build in builds]
    stale = True if True in stale_values else (None if None in stale_values else False)
    return (
        Build(
            build_id=first.build_id, instance_id=first.instance_id,
            instance_started_at=first.instance_started_at, stale_source=stale,
        ),
        stable,
    )


def _disclosures(scenario: ScenarioConfig, restart_undetectable: bool) -> list[str]:
    notes = list(scenario.disclosures)
    if scenario.mutates_data:
        notes.append("Scenario mutates data; state is reset before each trial.")
    if not scenario.user_confirmed:
        notes.append("Invariants not confirmed by user.")
    notes.append("State comes from the target's own hook. Client counts are measured by the runner.")
    if restart_undetectable:
        notes.append("Restart detection unavailable: the target doesn't report an instance_id.")
    return notes


def _reproduce(scenario: ScenarioConfig, plan: Plan) -> Reproduce:
    serial = "--serial-control" if plan.serial_control else "--no-serial-control"
    load = plan.load
    return Reproduce(
        cli=(
            f"faultforge run {scenario.id} --trials {plan.trials} {serial} "
            f"--vus {load.vus} --iterations {load.iterations} --max-duration-s {load.max_duration_s}"
        ),
        k6=f"k6 run --no-usage-report ... {scenario.script}.js",
    )


def _execute(
    plan: Plan,
    project: ProjectConfig,
    scenario: ScenarioConfig,
    hooks: TargetHooks,
    k6: Path,
    k6_version: str,
    hash_value: str,
    baseline: Report | None,
    project_dir: Path,
    limits: Limits,
    started: float,
) -> Report:
    deadline = started + limits.max_wall_s_total
    run_id = report_module.new_run_id()
    directory = report_module.run_dir(project_dir, run_id)
    directory.mkdir()
    requests_path = directory / report_module.REQUESTS_NAME
    requests_path.touch()

    kinds = ["concurrent"] * plan.trials + (["serial_control"] if plan.serial_control else [])
    serial_load = LoadConfig(vus=1, iterations=plan.load.iterations, max_duration_s=plan.load.max_duration_s)
    trials: list[TrialResult] = []
    records: list[RequestRecord] = []
    builds: list[Build] = []
    throughput: list[float] = []
    sent = 0
    exhausted = False
    restart_undetectable = False
    for number, kind in enumerate(kinds, start=1):
        load = serial_load if kind == "serial_control" else plan.load
        remaining_s = deadline - time.monotonic() - limits.trial_overhead_s
        if (
            exhausted
            or remaining_s < MIN_TRIAL_TIME_S
            or sent + load.iterations > limits.max_requests_total
        ):
            exhausted = True
            trials.append(_skipped(number, kind, load.iterations))
            continue
        outcome = _run_one_trial(
            number, kind, load, project, scenario, hooks, k6, directory, deadline, limits
        )
        trials.append(outcome.result)
        builds.extend(outcome.builds)
        exhausted = outcome.result.status == "skipped"
        restart_undetectable = restart_undetectable or outcome.restart_undetectable
        if outcome.engine is not None:
            records.extend(outcome.engine.records)
            sent += len(outcome.engine.records)
            if kind == "concurrent":
                throughput.append(outcome.engine.throughput_rps)

    completeness = _completeness(trials, plan.trials, plan.serial_control)
    violations = oracle.find_violations(scenario, trials, records)
    hypotheses = oracle.find_hypotheses(violations, trials, records)
    verdict, reason = oracle.decide_verdict(trials, completeness, violations)
    build, stable = _summarize_build(builds)
    report = Report(
        run_id=run_id,
        scenario_id=scenario.id,
        completeness=completeness,
        budget=Budget(
            max_requests_total=limits.max_requests_total,
            requests_planned=plan.planned_requests,
            requests_sent=sent,
            max_wall_s_total=limits.max_wall_s_total,
            wall_s_used=round(time.monotonic() - started, 2),
            clamped=list(plan.clamps),
        ),
        target=TargetInfo(origin=project.target.origin, build=build, build_stable_during_run=stable),
        config=RunConfig(
            load=plan.load,
            trials=plan.trials,
            serial_control=plan.serial_control,
            config_hash=hash_value,
            engine=Engine(name="k6", version=k6_version),
            mutates_data=scenario.mutates_data,
            user_confirmed=scenario.user_confirmed,
        ),
        trials=trials,
        verdict=verdict,
        verdict_reason=reason,
        violations=violations,
        hypotheses=hypotheses,
        performance=report_module.build_performance(trials, records, throughput),
        evidence=Evidence(requests=report_module.describe_evidence(requests_path)),
        reproduce=_reproduce(scenario, plan),
        disclosures=_disclosures(scenario, restart_undetectable),
    )
    if baseline is not None:
        report.comparison = report_module.compare_reports(baseline, report)
    report_module.write_report(directory, report)
    return report


def run_scenario(
    project_dir: Path,
    scenario_id: str,
    options: RunOptions = RunOptions(),
    roots: Iterable[Path] | None = None,
    limits: Limits = LIMITS,
    k6: Path | None = None,
) -> Report:
    """Raises FaultForgeError or NotComparable before any load is sent."""
    started = time.monotonic()
    project_dir = safety.resolve_project_dir(project_dir, safety.allowed_roots(roots))
    project = config_module.load_config(project_dir)
    scenario = config_module.find_scenario(project, scenario_id)
    try:
        k6 = k6 or k6_runner.find_k6()
    except K6Missing as error:
        raise FaultForgeError("k6_missing", str(error), "Run scripts/install_k6.sh.") from error

    baseline = None
    if options.compare_to is not None:
        baseline = report_module.read_report(project_dir, options.compare_to)
        if baseline.scenario_id != scenario.id:
            raise NotComparable(report_module.not_comparable(baseline.run_id, "scenario_id differs"))

    plan = resolve_plan(scenario, options, baseline, limits)
    admit(plan, limits)
    try:
        version = k6_runner.k6_version(k6)
    except (OSError, IndexError, subprocess.SubprocessError) as error:
        raise FaultForgeError("k6_missing", f"can't run {k6}: {error}", "Run scripts/install_k6.sh.") from error
    hash_value = config_hash(project, scenario, plan.load, version)
    if baseline is not None and baseline.config.config_hash != hash_value:
        raise NotComparable(report_module.not_comparable(baseline.run_id, "config_hash differs"))

    hooks = TargetHooks(project.target, limits.hook_timeout_s)
    try:
        with report_module.run_lock(project_dir):
            _preflight(hooks, scenario, project.target.origin)
            return _execute(
                plan, project, scenario, hooks, k6, version, hash_value, baseline, project_dir, limits, started
            )
    except ValidationError as error:
        # The evidence didn't fit the contract, so no verdict can be claimed from it.
        raise FaultForgeError("engine_failed", f"run produced invalid results: {error}") from error
    except OSError as error:
        raise FaultForgeError(
            "engine_failed", f"can't run or store results: {error}", "Check that the project is writable."
        ) from error


def describe_scenarios(
    project_dir: Path, roots: Iterable[Path] | None = None, limits: Limits = LIMITS
) -> dict[str, object]:
    """Read-only: never resets the target or sends load."""
    project_dir = safety.resolve_project_dir(project_dir, safety.allowed_roots(roots))
    project = config_module.load_config(project_dir)
    hooks = TargetHooks(project.target, limits.hook_timeout_s)
    try:
        hooks.health()
        snapshot = hooks.state()
        reachable, build = True, snapshot.build
    except HookError:
        reachable, build = False, None
    return {
        "target": {
            "origin": project.target.origin,
            "reachable": reachable,
            "build": build.model_dump() if build else None,
        },
        "scenarios": [
            {
                "id": scenario.id,
                "description": scenario.description,
                "mutates_data": scenario.mutates_data,
                "user_confirmed": scenario.user_confirmed,
                "load": scenario.load.model_dump(),
                "invariants": [invariant.name for invariant in scenario.invariants],
            }
            for scenario in project.scenarios
        ],
        "limits": limits.__dict__,
    }
