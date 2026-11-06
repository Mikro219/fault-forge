"""Decides what was proven, what is only suspected, and the verdict. All of it is deterministic.

Performance numbers never enter this module. Hypotheses never affect the verdict.
"""

from collections import Counter
from collections.abc import Sequence

from faultforge import expressions
from faultforge.models import (
    ClientCounts,
    Completeness,
    Hypothesis,
    InvariantConfig,
    InvariantResult,
    RequestRecord,
    ScenarioConfig,
    TrialResult,
    Verdict,
    Violation,
    ViolationEvidence,
)

VIOLATION_BY_INVARIANT_TYPE = {
    "capacity": "invariant.capacity_exceeded",
    "accounting": "invariant.accounting_mismatch",
    "consistency": "invariant.inconsistent_state",
    "orphan": "invariant.orphaned_resource",
    "integrity": "invariant.integrity_violated",
    "liveness": "invariant.liveness_failed",
    "bound": "invariant.bound_violated",
    "custom": "invariant.custom_failed",
}
HYPOTHESIS_RULES = {
    "invariant.accounting_mismatch": (
        "lost_update",
        "Look for a value written from an earlier read instead of an atomic update.",
    ),
    "invariant.orphaned_resource": (
        "missing_compensation",
        "Look for a failure or rejection path that doesn't release what was acquired.",
    ),
    "invariant.integrity_violated": (
        "required_step_bypassed",
        "Look for a code path that completes without its required step.",
    ),
    "invariant.liveness_failed": (
        "over_rejection",
        "Look for a check that refuses requests that should succeed.",
    ),
}
MAX_SAMPLES = 10


def evaluate_invariants(
    invariants: Sequence[InvariantConfig],
    client: ClientCounts,
    before: dict[str, int] | None,
    after: dict[str, int] | None,
    completed: bool,
) -> list[InvariantResult]:
    """On an incomplete trial only capacity checks count: state may be mid-transition."""
    values = {
        "client.accepted": client.accepted,
        "client.rejected": client.rejected,
        "client.unexpected": client.unexpected,
        "client.transport_error": client.transport_error,
        "client.total": client.accepted + client.rejected + client.unexpected + client.transport_error,
    }
    values.update({f"before.{key}": value for key, value in (before or {}).items()})
    values.update({f"after.{key}": value for key, value in (after or {}).items()})

    results = []
    for invariant in invariants:
        expression = expressions.parse(invariant.check)
        used = {name: values[name] for name in sorted(expression.variables) if name in values}
        if not completed and invariant.type != "capacity":
            results.append(InvariantResult(name=invariant.name, result="not_evaluable", values=used))
            continue
        try:
            passed = expression.evaluate(values)
        except expressions.NotEvaluable:
            results.append(InvariantResult(name=invariant.name, result="not_evaluable", values=used))
            continue
        results.append(
            InvariantResult(name=invariant.name, result="pass" if passed else "fail", values=used)
        )
    return results


def _accepted_overlap(records: Sequence[RequestRecord]) -> tuple[bool, float]:
    """Whether all accepted requests share a common instant, and how wide that window is."""
    accepted = [record for record in records if record.outcome == "accepted"]
    if len(accepted) < 2:
        return False, 0
    window = min(record.t_end for record in accepted) - max(record.t_start for record in accepted)
    return window > 0, max(window, 0)


def _observed(results: Sequence[InvariantResult]) -> dict[str, int]:
    observed = {}
    names = {name for result in results for name in result.values}
    for name in sorted(names):
        seen = {result.values[name] for result in results if name in result.values}
        if len(seen) == 1:
            observed[name] = seen.pop()
        else:
            observed[f"max_{name}"] = max(seen)
            observed[f"min_{name}"] = min(seen)
    return observed


def _sample(records: Sequence[RequestRecord], trial: int, outcome: str) -> ViolationEvidence:
    matching = [r for r in records if r.trial == trial and r.outcome == outcome]
    return ViolationEvidence(
        file="requests.jsonl",
        filter={"trial": trial, "outcome": outcome},
        sample=matching[:MAX_SAMPLES],
    )


def _trials_violated(violating: Sequence[TrialResult], evaluated: Sequence[TrialResult]) -> str:
    concurrent = [trial for trial in evaluated if trial.kind == "concurrent"]
    failing = [trial for trial in violating if trial.kind == "concurrent"]
    if failing:
        return f"{len(failing)}/{len(concurrent)}"
    return f"{len(violating)}/{len(evaluated) - len(concurrent)}"


def _invariant_violations(
    scenario: ScenarioConfig, trials: Sequence[TrialResult], records: Sequence[RequestRecord]
) -> list[Violation]:
    violations = []
    for invariant in scenario.invariants:
        evaluated, violating, failed = [], [], []
        for trial in trials:
            for result in trial.invariants:
                if result.name != invariant.name or result.result == "not_evaluable":
                    continue
                evaluated.append(trial)
                if result.result == "fail":
                    violating.append(trial)
                    failed.append(result)
        if not violating:
            continue
        first = next((t for t in violating if t.kind == "concurrent"), violating[0])
        facts: dict[str, bool | int | float | str] = {}
        if any(trial.kind == "serial_control" for trial in violating):
            facts["serial_control_violated"] = True
        trial_records = [record for record in records if record.trial == first.trial]
        if invariant.type == "capacity":
            overlap, window = _accepted_overlap(trial_records)
            facts["accepted_overlap"] = overlap
            facts["overlap_window_ms"] = window
        for status, count in sorted(Counter(r.status for r in trial_records).items()):
            facts[f"status_{status}"] = count
        violations.append(
            Violation(
                id=f"V{len(violations) + 1}",
                type=VIOLATION_BY_INVARIANT_TYPE[invariant.type],
                invariant=invariant.name,
                trials_violated=_trials_violated(violating, evaluated),
                observed=_observed(failed),
                facts=facts,
                evidence=_sample(records, first.trial, "accepted"),
            )
        )
    return violations


def _outcome_violations(
    trials: Sequence[TrialResult], records: Sequence[RequestRecord], next_id: int
) -> list[Violation]:
    """Only completed trials count. A restart or kill also breaks connections, so an incomplete
    trial's transport errors don't prove anything about the target."""
    completed = [trial for trial in trials if trial.status == "completed"]
    violations = []
    for violation_type, outcome, field in (
        ("response.unexpected_status", "unexpected", "unexpected"),
        ("transport.errors", "transport_error", "transport_error"),
    ):
        violating = [trial for trial in completed if getattr(trial.client, field) > 0]
        if not violating:
            continue
        trial_numbers = {trial.trial for trial in violating}
        statuses = Counter(
            record.status for record in records
            if record.trial in trial_numbers and record.outcome == outcome
        )
        violations.append(
            Violation(
                id=f"V{next_id + len(violations)}",
                type=violation_type,
                trials_violated=_trials_violated(violating, completed),
                observed={"total": sum(getattr(trial.client, field) for trial in violating)},
                facts={f"status_{status}": count for status, count in sorted(statuses.items())},
                evidence=_sample(records, violating[0].trial, outcome),
            )
        )
    return violations


def find_violations(
    scenario: ScenarioConfig, trials: Sequence[TrialResult], records: Sequence[RequestRecord]
) -> list[Violation]:
    violations = _invariant_violations(scenario, trials, records)
    return violations + _outcome_violations(trials, records, len(violations) + 1)


def _control_held(trials: Sequence[TrialResult], invariant: str | None) -> bool:
    for trial in trials:
        if trial.kind == "serial_control" and trial.status == "completed":
            return any(r.name == invariant and r.result == "pass" for r in trial.invariants)
    return False


def _hypothesis(
    index: int, cause: str, violation: Violation, basis: list[str], how: str, supported: bool
) -> Hypothesis:
    if supported:
        basis = [*basis, "serial control: invariant held"]
    return Hypothesis(
        id=f"H{index}",
        suspected_cause=cause,
        status="supported" if supported else "suspected",
        explains=[violation.id],
        basis=basis,
        how_to_confirm=how,
    )


def _has_concurrent_only_5xx(trials: Sequence[TrialResult], records: Sequence[RequestRecord]) -> bool:
    control = {t.trial for t in trials if t.kind == "serial_control" and t.status == "completed"}
    concurrent = {t.trial for t in trials if t.kind == "concurrent"}
    if not control:
        return False
    server_errors = {r.trial for r in records if 500 <= r.status <= 599}
    return bool(server_errors & concurrent) and not server_errors & control


def find_hypotheses(
    violations: Sequence[Violation], trials: Sequence[TrialResult], records: Sequence[RequestRecord]
) -> list[Hypothesis]:
    """Rule-generated suspects. 'supported' only means the failure depends on concurrency."""
    hypotheses: list[Hypothesis] = []
    for violation in violations:
        supported = _control_held(trials, violation.invariant)
        kind = violation.type.removeprefix("invariant.")
        if violation.type == "invariant.capacity_exceeded" and violation.facts.get("accepted_overlap"):
            hypotheses.append(
                _hypothesis(
                    len(hypotheses) + 1, "check_then_act_race", violation,
                    [kind, "accepted_overlap"],
                    "Look for a read of availability followed by an await or I/O before the write.",
                    supported,
                )
            )
        elif violation.type in HYPOTHESIS_RULES:
            cause, how = HYPOTHESIS_RULES[violation.type]
            hypotheses.append(
                _hypothesis(len(hypotheses) + 1, cause, violation, [kind], how, supported)
            )
        elif violation.type == "response.unexpected_status" and _has_concurrent_only_5xx(trials, records):
            hypotheses.append(
                Hypothesis(
                    id=f"H{len(hypotheses) + 1}",
                    suspected_cause="concurrency_fault",
                    status="supported",
                    explains=[violation.id],
                    basis=["5xx responses only in concurrent trials", "serial control: no 5xx"],
                    how_to_confirm="Look for lock contention or an unhandled conflict under load.",
                )
            )
    return hypotheses


def decide_verdict(
    trials: Sequence[TrialResult], completeness: Completeness, violations: Sequence[Violation]
) -> tuple[Verdict, str]:
    if all(trial.status == "skipped" for trial in trials):
        return "ERROR", "No trial started"
    if violations:
        count = len(violations)
        return "FAIL", f"{count} proven violation{'s' if count != 1 else ''}"
    results = [result.result for trial in trials for result in trial.invariants]
    planned = completeness.trials_planned
    done = completeness.trials_completed
    if completeness.status != "complete":
        return "INCONCLUSIVE", f"No proven violation, but only {done}/{planned} trials completed"
    if "not_evaluable" in results:
        return "INCONCLUSIVE", "No proven violation, but at least one invariant couldn't be evaluated"
    return "PASS", f"All invariants held in {done}/{planned} complete trials"
