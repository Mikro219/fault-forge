"""What the page may see: a deliberate allowlist mapped from SandboxResult and Report.

Everything from a target or a model is untrusted, and a report can carry it (captured values, build
strings, state keys, log tails, failure details). So nothing is copied by default. A field here
is a number, a fixed vocabulary, a name from the trusted config, or text written in this module.
The one exception is an attempt's diff, which the page asked for: it's model text, marked untrusted,
bounded, with control characters removed, and must only ever be shown as plain text.
"""

import difflib
import json
import math
import re
from collections.abc import Collection, Iterable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import ConfigDict, Field, ValidationError

from faultforge import report as report_module
from faultforge.errors import FaultForgeError
from faultforge.fix_agent import AttemptRecord, Proposal
from faultforge.models import (
    Contract,
    IncompleteReason,
    InvariantChange,
    InvariantOutcome,
    NotVerifiedReason,
    Outcome,
    Report,
    RequestRecord,
    SkipReason,
    SuspectedCause,
    TrialKind,
    TrialStatus,
    Verdict,
    ViolationType,
)
from faultforge.sandbox import ContainerFacts, ContainmentFacts, FailureReason, SandboxResult
from faultforge.web.targets import FixedTarget, TargetId

MAX_TIMELINE_POINTS = 200
MAX_REQUEST_LINES = 100_000
MAX_REQUESTS_BYTES = 64 * 1024 * 1024
MAX_DIFF_CHARS = 40_000
MAX_JS_INT = 2**53 - 1

SAFE_NAME = re.compile(r"[a-z][a-z0-9_]{0,47}")
SAFE_OBSERVED_NAME = re.compile(r"(?:(?:min|max)_)?(?:client|before|after)\.[a-z][a-z0-9_]{0,47}|total")
SAFE_VIOLATION_ID = re.compile(r"[VH]\d{1,6}")
SAFE_CODE = re.compile(r"[A-Za-z][A-Za-z0-9_]{0,63}")
SAFE_BUILD_ID = re.compile(r"[a-f0-9]{8,64}")
SAFE_SHA256 = re.compile(r"[a-f0-9]{64}")
SAFE_RUN_ID = re.compile(r"run_\d{8}_\d{6}_[a-f0-9]{4}")
SAFE_MODEL = re.compile(r"[a-z0-9][a-z0-9.\-]{0,63}")
SAFE_STATUS_CODE = re.compile(r"\d{1,3}")
SAFE_POLICY_LINE = re.compile(r"line \d{1,7}: must [ -~]{1,400}")
UNPRINTABLE = re.compile(r"[^\x20-\x7e\n\t]")

SHARED_KERNEL = "Sandboxes are containers, so they share this VM's kernel. gVisor isn't in use."

# The notes lifecycle.py adds to every report. tests/test_web.py keeps the two in step.
LIFECYCLE_DISCLOSURES = frozenset({
    "Scenario mutates data; state is reset before each trial.",
    "Invariants not confirmed by user.",
    "State comes from the target's own hook. Client counts are measured by the runner.",
    "Restart detection unavailable: the target doesn't report an instance_id.",
})

CAUSE_TEXT: Mapping[str, str] = {
    "check_then_act_race": "Requests may check availability and then act, with others getting in between.",
    "lost_update": "Concurrent writes may overwrite each other.",
    "missing_compensation": "A failed path may not undo an earlier step, such as a payment.",
    "required_step_bypassed": "A required step may be skipped on some path.",
    "over_rejection": "The service may reject requests it could have served.",
    "concurrency_fault": "Errors appeared only under concurrency, which may mean a conflict isn't handled.",
}

FAILURE_TEXT: Mapping[str, str] = {
    "invalid_spec": "The fixed target definition was refused.",
    "docker_unavailable": "Docker wasn't available on the server.",
    "image_missing": "The sandbox images aren't built on the server.",
    "target_start_failed": "The target's container couldn't start.",
    "target_exited": "The target exited before the load started.",
    "runner_start_failed": "The runner's container couldn't start.",
    "target_unreachable": "The target never answered inside its sandbox.",
    "not_comparable": "The retest couldn't be compared with its baseline.",
    "runner_error": "The runner stopped with an error.",
    "runner_oom": "The runner was killed at its memory limit.",
    "report_missing": "The runner finished without a report.",
    "report_invalid": "The runner's report failed validation, so it isn't shown.",
    "wall_limit_exceeded": "The host killed the run at its wall-clock limit.",
    "internal_error": "FaultForge hit an unexpected error during this run.",
    "shutdown": "The server shut down before this run could finish.",
}

PROBE_TEXT: Mapping[str, str] = {
    "egress_external": "Call an external server",
    "egress_dns": "Resolve an external name",
    "secrets_env": "Search the environment for keys",
    "docker_socket": "Reach the Docker socket",
    "write_outside_tmp": "Write outside its temporary directory",
    "privileges": "Hold Linux capabilities",
    "memory": "Exhaust memory",
    "fork": "Exhaust processes",
    "cpu_spin": "Spin the CPU",
    "guard": "Safety guard refused to run the attacks",
}
PROBE_NOTES: Mapping[str, str] = {
    "cpu_spin": "CPU use is throttled, not stopped. Only the host's wall-clock limit bounds it over time.",
}
PROBE_OUTCOMES = frozenset({"blocked", "not_blocked", "inconclusive"})

ATTEMPT_TEXT: Mapping[str, str] = {
    "policy_violation": "The code broke a code rule, so it was never run.",
    "candidate_fail": "The fix ran in a new sandbox, and FaultForge still proved a violation.",
    "candidate_inconclusive": "The fix ran in a new sandbox, but the result was inconclusive.",
    "candidate_error": "The fix ran in a new sandbox, but no trial started.",
    "not_verified": "The fix passed, but the comparison couldn't verify it against the baseline.",
    "state_mismatch": "What the service reported disagrees with its database.",
    "state_unavailable": "The service's database couldn't be checked.",
    "code_unchanged": "The code is identical to the failing baseline.",
    "budget_exhausted": "The budget would have been exceeded, so nothing was sent.",
    "auth_rejected": "The inference provider refused the request.",
    "rate_limited": "The inference provider kept rate limiting the request.",
    "http_error": "The inference provider returned an error.",
    "timeout": "The model call timed out.",
    "network_error": "The model call failed on the network.",
    "malformed_response": "The provider's response couldn't be read.",
    "response_too_large": "The provider's response was too large.",
    "empty_output": "The model returned nothing.",
    "output_truncated": "The model's output was cut off at its token limit.",
    "no_block": "The model's output had no complete code block.",
    "multiple_blocks": "The model's output had more than one code block.",
    "not_python": "The model's code block wasn't Python.",
    "empty_block": "The model's code block was empty.",
    "oversized": "The model's code was over the size limit.",
    "syntax_error": "The model's code didn't compile.",
    "invalid_utf8": "The model's code wasn't valid text.",
    "key_in_prompt": "The prompt was refused by a safety check, and nothing was sent.",
    "key_in_output": "The output was discarded by a safety check.",
    "unknown_model": "The model has no known price, so nothing was sent.",
    "internal_error": "FaultForge hit an unexpected error during this attempt.",
}

STOP_TEXT: Mapping[str, str] = {
    "fix_verified": "FaultForge verified the fix.",
    "attempts_exhausted": "No attempt passed, and the agent stopped at its attempt limit.",
    "baseline_not_fail": "There's no proven failure to fix.",
    "budget_exhausted": "The budget ran out.",
    "auth_rejected": "The inference provider refused the request.",
    "unknown_model": "The model has no known price.",
    "key_in_prompt": "A safety check stopped the agent.",
    "internal_error": "FaultForge hit an unexpected error during the fix.",
    "shutdown": "The server shut down before the fix could finish.",
}


class View(Contract):
    model_config = ConfigDict(extra="forbid", frozen=True)


class CompletenessView(View):
    status: Literal["complete", "incomplete"]
    trials_planned: int
    trials_completed: int
    trials_incomplete: int
    trials_skipped: int
    serial_control: Literal["completed", "incomplete", "skipped", "not_requested"]


class ContrastView(View):
    """One trial's numbers, for the headline. Which trial is named, so it isn't a cherry-pick."""

    trial: int
    accepted: int
    rejected: int
    unexpected: int
    transport_error: int
    stock_before: int | None
    stock_after: int | None
    orders: int | None = Field(description="Orders added during the trial")
    counts_source: Literal["measured_by_runner"] = "measured_by_runner"
    state_source: Literal["reported_by_target"] = "reported_by_target"


class ViolationView(View):
    id: str
    type: ViolationType
    proven: Literal[True] = True
    invariant: str | None
    trials_violated: str
    observed: dict[str, int | float]
    facts: dict[str, bool | int | float]


class CauseView(View):
    """A suspected cause: rule-generated, never proven, and never part of the verdict."""

    id: str
    cause: SuspectedCause
    status: Literal["suspected", "supported"]
    proven: Literal[False] = False
    explains: list[str]
    text: str


class TrialInvariantView(View):
    name: str | None
    result: InvariantOutcome


class TrialView(View):
    trial: int
    kind: TrialKind
    status: TrialStatus
    incomplete_reason: IncompleteReason | SkipReason | None
    planned_requests: int
    completed_requests: int
    peak_in_flight: int
    accepted: int
    rejected: int
    unexpected: int
    transport_error: int
    stock_before: int | None
    stock_after: int | None
    orders: int | None
    invariants: list[TrialInvariantView]


class TimelinePoint(View):
    offset_ms: float
    duration_ms: float
    outcome: Outcome


class TimelineView(View):
    trial: int
    total_requests: int
    truncated: bool
    points: list[TimelinePoint]


class LatencyView(View):
    p50: float | None
    p95: float | None
    p99: float | None
    max: float | None


class PerformanceView(View):
    observation_only: Literal[True] = True
    latency_ms: LatencyView
    latency_ms_by_outcome: dict[Outcome, LatencyView]
    throughput_rps_per_trial: list[float]
    error_rate: float | None
    rejection_rate: float | None
    status_counts: dict[str, int]


class InvariantComparisonView(View):
    name: str | None
    result: InvariantChange
    reason: NotVerifiedReason | None


class ComparisonView(View):
    baseline_run_id: str | None
    status: Literal["COMPARABLE", "NOT_COMPARABLE"]
    fix_verified: bool
    baseline_build_id: str | None
    candidate_build_id: str | None
    build_ids_self_reported: Literal[True] = True
    invariants: list[InvariantComparisonView]


class StateMismatchView(View):
    name: str
    reported: int | None
    database: int | None


class StateVerificationView(View):
    status: Literal["matched", "mismatch", "unavailable", "not_requested"]
    mismatches: list[StateMismatchView]
    reason: str | None


class ProbeView(View):
    """What the target says it did. Its free-text detail is never shown."""

    name: str
    label: str
    result: Literal["blocked", "not_blocked", "inconclusive", "no_result"]
    self_reported: Literal[True] = True
    note: str | None = None


class ContainerView(View):
    network: Literal["none", "target_namespace_only", "other"]
    read_only_rootfs: bool
    all_caps_dropped: bool
    privileged: bool
    no_new_privileges: bool
    non_root: bool
    memory_limit_mb: int
    cpu_limit: float
    pids_limit: int | None
    limits_applied: bool
    oom_killed: bool
    running_when_inspected: bool
    exit_code: int | None


class HostVerifiedView(View):
    """Read by the host from docker, independent of anything the target says."""

    source: Literal["host"] = "host"
    target: ContainerView | None
    runner: ContainerView | None
    containers_left: int | None
    wall_s: float | None
    wall_limit_s: float | None
    killed_at_wall_limit: bool


class FailureView(View):
    reason: str
    message: str


class RunView(View):
    """The final result of one sandbox run."""

    target: TargetId
    status: Literal["completed", "failed"]
    failure: FailureView | None
    run_id: str | None
    verdict: Verdict | None
    verdict_summary: str
    completeness: CompletenessView | None
    contrast: ContrastView | None
    violations: list[ViolationView]
    suspected_causes: list[CauseView]
    trials: list[TrialView]
    timeline: TimelineView | None
    performance: PerformanceView | None
    comparison: ComparisonView | None
    fix_verified: bool
    state_verification: StateVerificationView
    deployed_code_sha256: str | None
    target_build_id: str | None = Field(description="Reported by the target, and shown only if it looks like a hash")
    requests_sent: int | None
    probes: list[ProbeView] | None
    host_verified: HostVerifiedView
    disclosures: list[str]


class AttemptView(View):
    attempt: int
    model: str
    passed: bool
    failure_reason: str | None
    failure_text: str | None
    gate_failed: list[str]
    policy_violations: list[str]
    verdict: Verdict | None
    state_verification: str | None
    code_changed: bool
    diff: str | None = Field(description="Against the original app.py. Untrusted model text: show as plain text only.")
    diff_untrusted: Literal[True] = True
    diff_truncated: bool = False
    input_tokens: int
    output_tokens: int
    usage_reported: bool
    cost_usd: float
    candidate: RunView | None = None


class FixView(View):
    status: Literal["queued", "running", "verified", "not_verified", "failed"]
    model: str
    reasoning_effort: str
    max_attempts: int
    attempts: list[AttemptView]
    stop_reason: str | None
    stop_text: str | None
    total_cost_usd: float


class EventView(View):
    at: str
    name: str
    attempt: int | None = None


class RunStateView(View):
    """A live run while it's polled. Never a replay."""

    id: str
    replay: Literal[False] = False
    target: TargetId
    stage: Literal["queued", "running", "done", "failed", "fix_queued", "fixing"]
    queue_position: int | None
    created_at: str
    started_at: str | None
    finished_at: str | None
    events: list[EventView]
    result: RunView | None
    fix: FixView | None
    fix_available: bool


class ReplaySummaryView(View):
    id: str
    replay: Literal[True] = True
    recorded_at: str
    target: TargetId
    verdict: Verdict | None
    fix_verified: bool
    has_fix: bool


class ReplayView(View):
    """A stored real run. `replay` can't be false: the model refuses it, on the way in and out."""

    id: str
    replay: Literal[True] = True
    recorded_at: str
    target: TargetId
    result: RunView
    fix: FixView | None = None


class BudgetView(View):
    enabled: bool
    exhausted: bool
    daily_usd_spent: float
    daily_usd_cap: float
    total_usd_spent: float
    total_usd_cap: float
    daily_tokens_spent: int
    daily_token_cap: int


class VmView(View):
    healthy: bool
    docker_reachable: bool
    load_1m: float | None
    memory_available_mb: int | None
    disk_free_mb: int | None
    containers_left: int | None = Field(description="Labelled sandbox containers now. 2 is expected during a run.")


class TargetView(View):
    id: TargetId
    title: str
    description: str
    fixable: bool


class StatusView(View):
    busy: bool
    queue_length: int
    queue_limit: int
    cooldown_s: float
    targets: list[TargetView]
    agent_enabled: bool
    agent_disabled_reason: str | None
    budget: BudgetView
    vm: VmView
    replays_available: int


def timestamp(value: float) -> str:
    return datetime.fromtimestamp(value, UTC).isoformat()


# Mapping


def _int(value: object) -> int | None:
    return value if type(value) is int and abs(value) <= MAX_JS_INT else None


def _float(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if math.isfinite(value) else None


def _numbers(values: Mapping[str, object], allowed: re.Pattern[str]) -> dict[str, bool | int | float]:
    kept: dict[str, bool | int | float] = {}
    for name, value in sorted(values.items()):
        if not allowed.fullmatch(name):
            continue
        if isinstance(value, bool):
            kept[name] = value
        elif isinstance(value, int) and _int(value) is not None:
            kept[name] = value
        elif isinstance(value, float) and _float(value) is not None:
            kept[name] = value
    return kept


def _matching(value: object, pattern: re.Pattern[str]) -> str | None:
    return value if isinstance(value, str) and pattern.fullmatch(value) else None


def _known(name: object, names: Collection[str]) -> str | None:
    return name if isinstance(name, str) and name in names else None


def _orders(before: Mapping[str, int] | None, after: Mapping[str, int] | None) -> int | None:
    start, end = _int((before or {}).get("orders_confirmed")), _int((after or {}).get("orders_confirmed"))
    return end - start if start is not None and end is not None else None


def _trial_view(trial, names: Collection[str]) -> TrialView:
    return TrialView(
        trial=trial.trial,
        kind=trial.kind,
        status=trial.status,
        incomplete_reason=trial.incomplete_reason,
        planned_requests=trial.planned_requests,
        completed_requests=trial.completed_requests,
        peak_in_flight=trial.peak_in_flight,
        accepted=trial.client.accepted,
        rejected=trial.client.rejected,
        unexpected=trial.client.unexpected,
        transport_error=trial.client.transport_error,
        stock_before=_int((trial.state_before or {}).get("stock")),
        stock_after=_int((trial.state_after or {}).get("stock")),
        orders=_orders(trial.state_before, trial.state_after),
        invariants=[TrialInvariantView(name=_known(item.name, names), result=item.result) for item in trial.invariants],
    )


def _headline_trial(report: Report):
    """The first concurrent trial that completed, or failing that, the first that started."""
    concurrent = [trial for trial in report.trials if trial.kind == "concurrent" and trial.status != "skipped"]
    completed = [trial for trial in concurrent if trial.status == "completed"]
    return (completed or concurrent or [None])[0]


def _contrast(report: Report) -> ContrastView | None:
    trial = _headline_trial(report)
    if trial is None:
        return None
    return ContrastView(
        trial=trial.trial,
        accepted=trial.client.accepted,
        rejected=trial.client.rejected,
        unexpected=trial.client.unexpected,
        transport_error=trial.client.transport_error,
        stock_before=_int((trial.state_before or {}).get("stock")),
        stock_after=_int((trial.state_after or {}).get("stock")),
        orders=_orders(trial.state_before, trial.state_after),
    )


def _violation_view(violation, names: Collection[str]) -> ViolationView:
    return ViolationView(
        id=_matching(violation.id, SAFE_VIOLATION_ID) or "V0",
        type=violation.type,
        invariant=_known(violation.invariant, names),
        trials_violated=violation.trials_violated,
        observed=_numbers(violation.observed, SAFE_OBSERVED_NAME),
        facts=_numbers(violation.facts, SAFE_NAME),
    )


def _cause_view(hypothesis) -> CauseView:
    return CauseView(
        id=_matching(hypothesis.id, SAFE_VIOLATION_ID) or "H0",
        cause=hypothesis.suspected_cause,
        status=hypothesis.status,
        explains=[item for item in hypothesis.explains if SAFE_VIOLATION_ID.fullmatch(item)],
        text=CAUSE_TEXT[hypothesis.suspected_cause],
    )


def _request_lines(path: Path) -> Iterable[str]:
    if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_REQUESTS_BYTES:
        return
    with path.open(encoding="utf-8", errors="replace") as handle:
        for number, line in enumerate(handle):
            if number >= MAX_REQUEST_LINES:
                return
            yield line


def _refuse_constant(name: str) -> None:
    raise ValueError(f"{name} isn't JSON")


def read_timeline(project_dir: Path, report: Report, trial_number: int) -> TimelineView | None:
    """A bounded, evenly spaced sample of one trial: times and outcome only. Each line is validated,
    and everything else on it, including the captured value, is dropped here."""
    try:
        path = report_module.run_dir(project_dir, report.run_id) / report_module.REQUESTS_NAME
        rows = []
        for line in _request_lines(path):
            try:
                record = RequestRecord.model_validate(json.loads(line, parse_constant=_refuse_constant))
            except (ValueError, ValidationError, RecursionError):
                continue
            if record.trial != trial_number:
                continue
            duration = record.t_end - record.t_start
            if math.isfinite(record.t_start) and math.isfinite(duration):
                rows.append((record.t_start, max(duration, 0.0), record.outcome))
    except (FaultForgeError, OSError):
        return None
    if not rows:
        return None
    rows.sort(key=lambda row: row[0])
    start = rows[0][0]
    step = max(1, math.ceil(len(rows) / MAX_TIMELINE_POINTS))
    points = [
        TimelinePoint(offset_ms=round(row[0] - start, 1), duration_ms=round(row[1], 1), outcome=row[2])
        for row in rows[::step]
    ]
    return TimelineView(trial=trial_number, total_requests=len(rows), truncated=step > 1, points=points)


def _latency_view(latency) -> LatencyView:
    return LatencyView(p50=_float(latency.p50), p95=_float(latency.p95), p99=_float(latency.p99), max=_float(latency.max))


def _performance_view(report: Report) -> PerformanceView | None:
    performance = report.performance
    if performance is None:
        return None
    return PerformanceView(
        latency_ms=_latency_view(performance.latency_ms),
        latency_ms_by_outcome={name: _latency_view(item) for name, item in performance.latency_ms_by_outcome.items()},
        throughput_rps_per_trial=[item for item in map(_float, performance.throughput_rps_per_trial) if item is not None],
        error_rate=_float(performance.error_rate),
        rejection_rate=_float(performance.rejection_rate),
        status_counts={name: count for name, count in performance.status_counts.items() if SAFE_STATUS_CODE.fullmatch(name)},
    )


def _comparison_view(report: Report, names: Collection[str]) -> ComparisonView | None:
    comparison = report.comparison
    if comparison is None:
        return None
    return ComparisonView(
        baseline_run_id=_matching(comparison.baseline_run_id, SAFE_RUN_ID),
        status=comparison.status,
        fix_verified=comparison.fix_verified,
        baseline_build_id=_matching(comparison.baseline_build_id, SAFE_BUILD_ID),
        candidate_build_id=_matching(comparison.candidate_build_id, SAFE_BUILD_ID),
        invariants=[
            InvariantComparisonView(name=_known(item.name, names), result=item.result, reason=item.reason)
            for item in comparison.invariants
        ],
    )


def _state_verification_view(containment: ContainmentFacts) -> StateVerificationView:
    facts = containment.state_verification
    if facts is None:
        return StateVerificationView(status="not_requested", mismatches=[], reason=None)
    return StateVerificationView(
        status=facts.status,
        mismatches=[
            StateMismatchView(name=item.name, reported=_int(item.reported), database=_int(item.database))
            for item in facts.mismatches
            if SAFE_NAME.fullmatch(item.name)
        ],
        reason=_matching(facts.reason, SAFE_CODE),
    )


def _probe_views(containment: ContainmentFacts) -> list[ProbeView]:
    """In a fixed order under fixed names, so a target can't add a row or reorder them. A probe that
    printed started and never a result was killed or removed during it."""
    results: dict[str, str] = {}
    started: set[str] = set()
    for probe in containment.probes:
        name = _known(probe.get("probe"), PROBE_TEXT)
        if name is None:
            continue
        if probe.get("phase") == "started":
            started.add(name)
        elif probe.get("phase") == "result":
            outcome = _known(probe.get("outcome"), PROBE_OUTCOMES)
            if outcome is not None:
                results[name] = outcome
    return [
        ProbeView(
            name=name, label=label, result=results.get(name, "no_result"), note=PROBE_NOTES.get(name),
        )
        for name, label in PROBE_TEXT.items()
        if name in results or name in started
    ]


def _container_view(facts: ContainerFacts | None, role: Literal["target", "runner"]) -> ContainerView | None:
    if facts is None:
        return None
    if role == "target":
        network = "none" if facts.network_mode == "none" else "other"
    else:
        network = "target_namespace_only" if facts.network_mode.startswith("container:") else "other"
    return ContainerView(
        network=network,
        read_only_rootfs=facts.read_only_rootfs,
        all_caps_dropped="ALL" in facts.cap_drop and not facts.cap_add,
        privileged=facts.privileged,
        no_new_privileges=facts.no_new_privileges,
        non_root=not facts.user.startswith(("0:", "root")) and bool(facts.user),
        memory_limit_mb=facts.memory_bytes // (1024 * 1024),
        cpu_limit=facts.nano_cpus / 1_000_000_000,
        pids_limit=facts.pids_limit,
        limits_applied=facts.limits_applied,
        oom_killed=facts.oom_killed,
        running_when_inspected=facts.running_when_inspected,
        exit_code=_int(facts.exit_code),
    )


def _host_verified(result: SandboxResult) -> HostVerifiedView:
    facts = result.containment
    return HostVerifiedView(
        target=_container_view(facts.target, "target"),
        runner=_container_view(facts.runner, "runner"),
        containers_left=facts.containers_left,
        wall_s=_float(facts.wall_s),
        wall_limit_s=_float(facts.wall_limit_s),
        killed_at_wall_limit=result.failure_reason == "wall_limit_exceeded",
    )


def _verdict_summary(report: Report) -> str:
    """Written here from counts, rather than copied from the report."""
    done, planned = report.completeness.trials_completed, report.completeness.trials_planned
    if report.verdict == "FAIL":
        count = len(report.violations)
        return f"{count} proven violation{'s' if count != 1 else ''}."
    if report.verdict == "PASS":
        return f"All invariants held in {done} of {planned} complete trials."
    if report.verdict == "INCONCLUSIVE":
        return f"No proven violation, but {done} of {planned} trials completed. This isn't a pass."
    return "No trial started."


def build_run_view(result: SandboxResult, target: FixedTarget, project_dir: Path) -> RunView:
    report = result.report if result.status == "completed" else None
    names = target.invariant_names
    allowed_disclosures = target.disclosures | LIFECYCLE_DISCLOSURES
    disclosures = [item for item in report.disclosures if item in allowed_disclosures] if report else []
    probes = _probe_views(result.containment) if target.spec.allow_hostile else None
    failure = None
    if result.status == "failed":
        reason = result.failure_reason or "internal_error"
        failure = FailureView(reason=reason, message=FAILURE_TEXT.get(reason, FAILURE_TEXT["internal_error"]))
    headline = _headline_trial(report) if report else None
    return RunView(
        target=target.id,
        status=result.status,
        failure=failure,
        run_id=report.run_id if report else None,
        verdict=report.verdict if report else None,
        verdict_summary=_verdict_summary(report) if report else failure.message if failure else "No report.",
        completeness=CompletenessView(**report.completeness.model_dump()) if report else None,
        contrast=_contrast(report) if report else None,
        violations=[_violation_view(item, names) for item in report.violations] if report else [],
        suspected_causes=[_cause_view(item) for item in report.hypotheses] if report else [],
        trials=[_trial_view(item, names) for item in report.trials] if report else [],
        timeline=read_timeline(project_dir, report, headline.trial) if report and headline else None,
        performance=_performance_view(report) if report else None,
        comparison=_comparison_view(report, names) if report else None,
        fix_verified=bool(report and report.comparison and report.comparison.fix_verified),
        state_verification=_state_verification_view(result.containment),
        deployed_code_sha256=_matching(result.deployed_code_sha256, SAFE_SHA256),
        target_build_id=_matching(report.target.build.build_id, SAFE_BUILD_ID) if report else None,
        requests_sent=report.budget.requests_sent if report else None,
        probes=probes,
        host_verified=_host_verified(result),
        disclosures=[*disclosures, SHARED_KERNEL],
    )


def internal_failure_view(target: FixedTarget, reason: FailureReason | Literal["shutdown"]) -> RunView:
    """For a run that ended with no SandboxResult at all."""
    message = FAILURE_TEXT[reason]
    return RunView(
        target=target.id, status="failed", failure=FailureView(reason=reason, message=message), run_id=None,
        verdict=None, verdict_summary=message, completeness=None, contrast=None, violations=[], suspected_causes=[],
        trials=[], timeline=None, performance=None, comparison=None, fix_verified=False,
        state_verification=StateVerificationView(status="unavailable", mismatches=[], reason="no_report"),
        deployed_code_sha256=None, target_build_id=None, requests_sent=None,
        probes=[] if target.spec.allow_hostile else None,
        host_verified=HostVerifiedView(
            target=None, runner=None, containers_left=None, wall_s=None, wall_limit_s=None, killed_at_wall_limit=False,
        ),
        disclosures=[SHARED_KERNEL],
    )


def attempt_diff(original: bytes, code: str | None) -> tuple[str | None, bool]:
    if code is None:
        return None, False
    lines = difflib.unified_diff(
        original.decode("utf-8", "replace").splitlines(), code.splitlines(),
        "app.py (original)", "app.py (candidate)", lineterm="", n=2,
    )
    text = UNPRINTABLE.sub("?", "\n".join(lines))
    return text[:MAX_DIFF_CHARS], len(text) > MAX_DIFF_CHARS


def attempt_failure_text(reason: str | None) -> str | None:
    if reason is None:
        return None
    if reason.startswith("sandbox_"):
        return "The sandbox failed while testing this fix."
    return ATTEMPT_TEXT.get(reason, "This attempt failed.")


def build_attempt_view(
    record: AttemptRecord, proposal: Proposal, original: bytes, candidate: RunView | None,
) -> AttemptView:
    diff, truncated = attempt_diff(original, proposal.code)
    return AttemptView(
        attempt=record.attempt,
        model=_matching(record.model, SAFE_MODEL) or "unknown",
        passed=record.fix_verified,
        failure_reason=_matching(record.failure_reason, SAFE_CODE),
        failure_text=attempt_failure_text(record.failure_reason),
        gate_failed=[item for item in record.gate_failed if SAFE_NAME.fullmatch(item)],
        policy_violations=[item for item in record.policy_violations if SAFE_POLICY_LINE.fullmatch(item)],
        verdict=record.verdict,
        state_verification=record.state_verification,
        code_changed=record.code_changed,
        diff=diff,
        diff_truncated=truncated,
        input_tokens=record.input_tokens,
        output_tokens=record.output_tokens,
        usage_reported=record.usage_reported,
        cost_usd=record.cost_usd,
        candidate=candidate,
    )
