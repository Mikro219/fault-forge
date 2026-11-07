"""The FaultForge contracts: scenario config, per-request evidence, trials and reports.

These models are the source of truth for report.json (docs/PLAN.md §4). Validators
enforce the verdict rules, so a report that claims more than its evidence supports
can't be constructed.
"""

from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

SCHEMA_VERSION = "2.1"
MAX_SAMPLES_PER_VIOLATION = 10
MAX_STATUSES = 32

Verdict = Literal["PASS", "FAIL", "INCONCLUSIVE", "ERROR"]
Outcome = Literal["accepted", "rejected", "unexpected", "transport_error"]
InvariantType = Literal[
    "capacity", "accounting", "consistency", "orphan", "integrity", "liveness", "bound", "custom"
]
InvariantOutcome = Literal["pass", "fail", "not_evaluable"]
TrialKind = Literal["concurrent", "serial_control"]
TrialStatus = Literal["completed", "incomplete", "skipped"]
IncompleteReason = Literal[
    "timeout",
    "iterations_short",
    "engine_exit_nonzero",
    "target_restarted",
    "reset_failed",
    "state_snapshot_failed",
    "insufficient_contention",
    "request_count_mismatch",
]
SkipReason = Literal["budget_exhausted"]
ViolationType = Literal[
    "invariant.capacity_exceeded",
    "invariant.accounting_mismatch",
    "invariant.inconsistent_state",
    "invariant.orphaned_resource",
    "invariant.integrity_violated",
    "invariant.liveness_failed",
    "invariant.bound_violated",
    "invariant.custom_failed",
    "response.unexpected_status",
    "transport.errors",
]
SuspectedCause = Literal[
    "check_then_act_race",
    "lost_update",
    "missing_compensation",
    "required_step_bypassed",
    "over_rejection",
    "concurrency_fault",
]
InvariantChange = Literal["FIXED", "NOT_VERIFIED", "REGRESSED", "STILL_FAILING", "STILL_PASSING"]
UNRESOLVED_CHANGES = {"NOT_VERIFIED", "REGRESSED", "STILL_FAILING"}
NotVerifiedReason = Literal[
    "trials_incomplete", "build_unchanged", "build_unknown", "stale_source", "not_evaluable"
]


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid")


# Scenario configuration (faultforge.yaml)


class ResetHook(Contract):
    method: Literal["POST"]
    path: str
    json_body: dict[str, object] = Field(default_factory=dict, alias="json")


class TargetConfig(Contract):
    origin: str
    health: str
    state: str
    reset: ResetHook | None = None
    # Where health, state and reset live, for an app that has no test hooks of its own.
    hooks_origin: str | None = None


class RequestTemplate(Contract):
    method: Literal["GET", "POST", "PUT", "PATCH", "DELETE"]
    path: str
    json_body: dict[str, object] | None = Field(default=None, alias="json")


class OutcomeConfig(Contract):
    accepted_status: list[int] = Field(min_length=1, max_length=MAX_STATUSES)
    rejected_status: list[int] = Field(default_factory=list, max_length=MAX_STATUSES)
    capture_field: str | None = None

    @model_validator(mode="after")
    def statuses_are_disjoint(self) -> Self:
        if set(self.accepted_status) & set(self.rejected_status):
            raise ValueError("a status can't be both accepted and rejected")
        return self


class LoadConfig(Contract):
    vus: int = Field(ge=1)
    iterations: int = Field(ge=1)
    max_duration_s: int = Field(ge=1)
    min_peak_in_flight: int | None = Field(default=None, ge=2)

    @model_validator(mode="after")
    def every_vu_has_an_iteration(self) -> Self:
        if self.iterations < self.vus:
            raise ValueError("iterations must be at least vus")
        return self

    @property
    def effective_min_peak_in_flight(self) -> int:
        if self.min_peak_in_flight is not None:
            return self.min_peak_in_flight
        return max(2, self.vus // 2)


class InvariantConfig(Contract):
    name: str
    type: InvariantType
    check: str


class ScenarioConfig(Contract):
    id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]*$")
    description: str
    mutates_data: bool
    user_confirmed: bool = False
    script: str
    request: RequestTemplate
    outcomes: OutcomeConfig
    load: LoadConfig
    # Replaces target.reset's body for this scenario, such as a different starting stock.
    reset_json: dict[str, object] | None = None
    invariants: list[InvariantConfig] = Field(min_length=1)
    disclosures: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def invariant_names_are_unique(self) -> Self:
        names = [invariant.name for invariant in self.invariants]
        if len(names) != len(set(names)):
            raise ValueError("invariant names must be unique within a scenario")
        return self


class ProjectConfig(Contract):
    version: Literal[1]
    target: TargetConfig
    scenarios: list[ScenarioConfig] = Field(min_length=1)

    @model_validator(mode="after")
    def scenarios_are_safe_to_run(self) -> Self:
        ids = [scenario.id for scenario in self.scenarios]
        if len(ids) != len(set(ids)):
            raise ValueError("scenario ids must be unique")
        if self.target.reset is None and any(s.mutates_data for s in self.scenarios):
            raise ValueError("mutating scenarios require target.reset")
        if self.target.reset is None and any(s.reset_json is not None for s in self.scenarios):
            raise ValueError("reset_json requires target.reset")
        return self


# Per-request evidence (requests.jsonl)


class RequestRecord(Contract):
    trial: int = Field(ge=1)
    request_id: str
    vu: int = Field(ge=1)
    iter: int = Field(ge=0)
    t_start: float = Field(description="Epoch milliseconds")
    t_end: float = Field(description="Epoch milliseconds")
    latency_ms: float = Field(ge=0)
    status: int = Field(ge=0, description="0 when the request failed in transport")
    outcome: Outcome
    captured: str | None = None


# Trials


class ClientCounts(Contract):
    accepted: int = Field(ge=0)
    rejected: int = Field(ge=0)
    unexpected: int = Field(ge=0)
    transport_error: int = Field(ge=0)


class InvariantResult(Contract):
    name: str
    result: InvariantOutcome
    values: dict[str, int] = Field(default_factory=dict)


class TrialResult(Contract):
    trial: int = Field(ge=1)
    kind: TrialKind
    status: TrialStatus
    incomplete_reason: IncompleteReason | SkipReason | None = None
    planned_requests: int = Field(ge=0)
    completed_requests: int = Field(ge=0)
    peak_in_flight: int = Field(default=0, ge=0)
    client: ClientCounts
    state_before: dict[str, int] | None = None
    state_after: dict[str, int] | None = None
    invariants: list[InvariantResult] = Field(default_factory=list)

    @model_validator(mode="after")
    def reason_matches_status(self) -> Self:
        if self.status == "completed" and self.incomplete_reason is not None:
            raise ValueError("a completed trial can't have an incomplete_reason")
        if self.status == "incomplete" and self.incomplete_reason in (None, "budget_exhausted"):
            raise ValueError("an incomplete trial needs an incomplete reason")
        if self.status == "skipped" and self.incomplete_reason != "budget_exhausted":
            raise ValueError("a skipped trial must have reason budget_exhausted")
        return self


# Report sections


class Completeness(Contract):
    status: Literal["complete", "incomplete"]
    trials_planned: int = Field(ge=0, description="Concurrent trials; excludes the serial control")
    trials_completed: int = Field(ge=0)
    trials_incomplete: int = Field(ge=0)
    trials_skipped: int = Field(ge=0)
    serial_control: Literal["completed", "incomplete", "skipped", "not_requested"]

    @model_validator(mode="after")
    def counts_add_up(self) -> Self:
        total = self.trials_completed + self.trials_incomplete + self.trials_skipped
        if total != self.trials_planned:
            raise ValueError("trial counts don't add up to trials_planned")
        is_complete = (
            self.trials_completed == self.trials_planned
            and self.serial_control in ("completed", "not_requested")
        )
        if (self.status == "complete") != is_complete:
            raise ValueError("completeness status doesn't match the trial counts")
        return self


class Clamp(Contract):
    field: Literal["vus", "iterations", "max_duration_s"]
    requested: int
    effective: int


class Budget(Contract):
    max_requests_total: int = Field(ge=1)
    requests_planned: int = Field(ge=0)
    requests_sent: int = Field(ge=0)
    max_wall_s_total: float = Field(gt=0)
    wall_s_used: float = Field(ge=0)
    clamped: list[Clamp] = Field(default_factory=list)


class Build(Contract):
    build_id: str | None
    instance_id: str | None
    instance_started_at: str | None
    stale_source: bool | None


class TargetInfo(Contract):
    origin: str
    build: Build
    build_stable_during_run: bool | None


class Engine(Contract):
    name: str
    version: str


class RunConfig(Contract):
    load: LoadConfig
    trials: int = Field(ge=0)
    serial_control: bool
    config_hash: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    engine: Engine
    mutates_data: bool
    user_confirmed: bool


class ViolationEvidence(Contract):
    file: str
    filter: dict[str, str | int] = Field(default_factory=dict)
    sample: list[RequestRecord] = Field(default_factory=list, max_length=MAX_SAMPLES_PER_VIOLATION)


class Violation(Contract):
    """A proven fact, derived deterministically from responses and state."""

    id: str = Field(pattern=r"^V\d+$")
    type: ViolationType
    proven: Literal[True] = True
    invariant: str | None = None
    trials_violated: str = Field(pattern=r"^\d+/\d+$")
    observed: dict[str, int | float] = Field(default_factory=dict)
    facts: dict[str, bool | int | float | str] = Field(default_factory=dict)
    evidence: ViolationEvidence | None = None


class Hypothesis(Contract):
    """A suspected cause. Rule-generated, never proven, and never affects the verdict."""

    id: str = Field(pattern=r"^H\d+$")
    suspected_cause: SuspectedCause
    status: Literal["suspected", "supported"]
    explains: list[str] = Field(min_length=1)
    basis: list[str] = Field(min_length=1)
    how_to_confirm: str


class Latency(Contract):
    p50: float = Field(ge=0)
    p95: float = Field(ge=0)
    p99: float | None = Field(default=None, ge=0)
    max: float | None = Field(default=None, ge=0)


class Performance(Contract):
    """Aggregates only. No value here affects the verdict."""

    scope: str
    latency_ms: Latency
    latency_ms_by_outcome: dict[Outcome, Latency] = Field(default_factory=dict)
    throughput_rps_per_trial: list[float] = Field(default_factory=list)
    error_rate: float = Field(ge=0, le=1)
    rejection_rate: float = Field(ge=0, le=1)
    status_counts: dict[str, int] = Field(default_factory=dict)


class EvidenceFile(Contract):
    path: str
    count: int = Field(ge=0)
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")


class Evidence(Contract):
    requests: EvidenceFile | None = None


class InvariantComparison(Contract):
    name: str
    result: InvariantChange
    reason: NotVerifiedReason | None = None

    @model_validator(mode="after")
    def reason_only_when_not_verified(self) -> Self:
        if (self.result == "NOT_VERIFIED") != (self.reason is not None):
            raise ValueError("a reason is required for NOT_VERIFIED, and only for NOT_VERIFIED")
        return self


class Comparison(Contract):
    baseline_run_id: str
    status: Literal["COMPARABLE", "NOT_COMPARABLE"]
    not_comparable_reason: str | None = None
    baseline_build_id: str | None = None
    candidate_build_id: str | None = None
    invariants: list[InvariantComparison] = Field(default_factory=list)
    fix_verified: bool = Field(
        default=False,
        description="The run-level answer: candidate PASS, an invariant FIXED, and none left unresolved",
    )
    performance_delta: dict[str, float] = Field(default_factory=dict)

    @model_validator(mode="after")
    def not_comparable_has_no_results(self) -> Self:
        if self.status == "NOT_COMPARABLE" and (self.invariants or not self.not_comparable_reason):
            raise ValueError("NOT_COMPARABLE needs a reason and no invariant results")
        return self

    @model_validator(mode="after")
    def fix_verified_matches_results(self) -> Self:
        results = {item.result for item in self.invariants}
        if self.fix_verified and ("FIXED" not in results or results & UNRESOLVED_CHANGES):
            raise ValueError("fix_verified requires a FIXED invariant and none unresolved")
        return self


class Reproduce(Contract):
    cli: str
    k6: str | None = None


# Top-level results


class RunResult(Contract):
    """What the lifecycle measured, before the oracle draws any conclusion."""

    run_id: str = Field(pattern=r"^run_\d{8}_\d{6}_[a-f0-9]{4}$")
    scenario_id: str
    completeness: Completeness
    budget: Budget
    target: TargetInfo
    config: RunConfig
    trials: list[TrialResult] = Field(default_factory=list)


class Report(RunResult):
    """The report.json contract: a RunResult plus the oracle's conclusions."""

    schema_version: Literal["2.1"] = SCHEMA_VERSION
    verdict: Verdict
    verdict_reason: str
    violations: list[Violation] = Field(default_factory=list)
    hypotheses: list[Hypothesis] = Field(default_factory=list)
    performance: Performance | None = None
    evidence: Evidence = Field(default_factory=Evidence)
    comparison: Comparison | None = None
    reproduce: Reproduce | None = None
    disclosures: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def hypotheses_explain_real_violations(self) -> Self:
        violation_ids = {violation.id for violation in self.violations}
        for hypothesis in self.hypotheses:
            unknown = set(hypothesis.explains) - violation_ids
            if unknown:
                raise ValueError(f"{hypothesis.id} explains unknown violations: {sorted(unknown)}")
        return self

    @model_validator(mode="after")
    def concurrent_trials_exercised_contention(self) -> Self:
        required = self.config.load.effective_min_peak_in_flight
        for trial in self.trials:
            if (
                trial.kind == "concurrent"
                and trial.status == "completed"
                and trial.peak_in_flight < required
            ):
                raise ValueError(
                    f"trial {trial.trial} is completed with peak_in_flight "
                    f"{trial.peak_in_flight}, below the required {required}"
                )
        return self

    @model_validator(mode="after")
    def verdict_matches_evidence(self) -> Self:
        started = [trial for trial in self.trials if trial.status != "skipped"]
        results = [inv.result for trial in self.trials for inv in trial.invariants]

        if self.verdict != "FAIL" and self.violations:
            raise ValueError(f"{self.verdict} can't include proven violations")
        if self.verdict == "FAIL" and not self.violations:
            raise ValueError("FAIL requires at least one proven violation")
        if self.verdict == "ERROR" and started:
            raise ValueError("ERROR means no trial started")
        if self.verdict == "PASS":
            self._require_pass_evidence(results)
        if self.verdict == "INCONCLUSIVE":
            if self.completeness.status == "complete" and "not_evaluable" not in results:
                raise ValueError("INCONCLUSIVE needs an incomplete trial or a not_evaluable invariant")
        if self.comparison is not None and self.comparison.fix_verified and self.verdict != "PASS":
            raise ValueError("fix_verified requires the candidate's verdict to be PASS")
        return self

    def _require_pass_evidence(self, results: list[InvariantOutcome]) -> None:
        if self.completeness.status != "complete":
            raise ValueError("PASS requires every planned trial to complete")
        if not self.trials or any(trial.status != "completed" for trial in self.trials):
            raise ValueError("PASS requires every planned trial to complete")
        if any(t.planned_requests == 0 or t.completed_requests != t.planned_requests for t in self.trials):
            raise ValueError("PASS requires every planned request to complete in every trial")
        if sum(trial.kind == "concurrent" for trial in self.trials) != self.completeness.trials_planned:
            raise ValueError("PASS requires as many concurrent trials as were planned")
        if any(not trial.invariants for trial in self.trials):
            raise ValueError("PASS requires invariants evaluated in every trial")
        if any(result != "pass" for result in results):
            raise ValueError("PASS requires every invariant to pass")
        if any(t.client.unexpected or t.client.transport_error for t in self.trials):
            raise ValueError("PASS can't include unexpected or transport outcomes")
