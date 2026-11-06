"""Aggregates evidence, compares runs, renders the report and reads and writes run artifacts."""

import fcntl
import hashlib
import json
import math
import os
import tempfile
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from pydantic import ValidationError

from faultforge import safety
from faultforge.errors import FaultForgeError
from faultforge.evidence import count_outcomes
from faultforge.models import (
    UNRESOLVED_CHANGES,
    Comparison,
    EvidenceFile,
    InvariantComparison,
    Latency,
    Performance,
    Report,
    RequestRecord,
    TrialResult,
)

STORE_DIR = ".faultforge"
RUNS_DIR = "runs"
REPORT_NAME = "report.json"
SUMMARY_NAME = "report.md"
REQUESTS_NAME = "requests.jsonl"
ARTIFACT_MODE = 0o644
BLACK_BOX_NOTICE = (
    "FaultForge observes black-box behaviour; root cause must be confirmed in code."
)


def percentile(sorted_values: Sequence[float], fraction: float) -> float:
    """Linear interpolation, the same method k6 uses."""
    position = (len(sorted_values) - 1) * fraction
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return sorted_values[lower]
    return sorted_values[lower] + (sorted_values[upper] - sorted_values[lower]) * (position - lower)


def _latency(values: Sequence[float]) -> Latency:
    ordered = sorted(values)
    return Latency(
        p50=round(percentile(ordered, 0.5), 3),
        p95=round(percentile(ordered, 0.95), 3),
        p99=round(percentile(ordered, 0.99), 3),
        max=round(ordered[-1], 3),
    )


def build_performance(
    trials: Sequence[TrialResult], records: Sequence[RequestRecord], throughput: Sequence[float]
) -> Performance | None:
    """Percentiles can't be averaged across trials, so they come from the raw evidence."""
    concurrent = {trial.trial for trial in trials if trial.kind == "concurrent"}
    scoped = [record for record in records if record.trial in concurrent]
    if not scoped:
        return None
    counts = count_outcomes(scoped)
    by_outcome = {}
    for outcome in ("accepted", "rejected", "unexpected", "transport_error"):
        values = [record.latency_ms for record in scoped if record.outcome == outcome]
        if values:
            by_outcome[outcome] = _latency(values)
    statuses: dict[str, int] = {}
    for record in scoped:
        statuses[str(record.status)] = statuses.get(str(record.status), 0) + 1
    return Performance(
        scope="requests of concurrent trials; observation only, doesn't affect the verdict",
        latency_ms=_latency([record.latency_ms for record in scoped]),
        latency_ms_by_outcome=by_outcome,
        throughput_rps_per_trial=[round(value, 2) for value in throughput],
        error_rate=round((counts.unexpected + counts.transport_error) / len(scoped), 4),
        rejection_rate=round(counts.rejected / len(scoped), 4),
        status_counts=dict(sorted(statuses.items())),
    )


# Comparison


def _invariant_outcomes(report: Report) -> dict[str, str]:
    """Across a run, one failure is a failure. Otherwise any gap makes it not_evaluable."""
    per_trial = [{result.name: result.result for result in trial.invariants} for trial in report.trials]
    outcomes: dict[str, str] = {}
    for results in per_trial:
        for name in results:
            seen = [other.get(name) for other in per_trial]
            if "fail" in seen:
                outcomes[name] = "fail"
            elif all(outcome == "pass" for outcome in seen):
                outcomes[name] = "pass"
            else:
                outcomes[name] = "not_evaluable"
    return outcomes


def _not_verified_reason(baseline: Report, candidate: Report) -> str | None:
    if candidate.completeness.status != "complete":
        return "trials_incomplete"
    if candidate.target.build.stale_source is True:
        return "stale_source"
    if not candidate.target.build_stable_during_run or not baseline.target.build_stable_during_run:
        return "build_unknown"
    old, new = baseline.target.build.build_id, candidate.target.build.build_id
    if old is None or new is None:
        return "build_unknown"
    if old == new:
        return "build_unchanged"
    if candidate.target.build.stale_source is not False:
        return "stale_source"
    return None


def _compare_invariant(name: str, old: str | None, new: str | None, baseline: Report, candidate: Report) -> InvariantComparison:
    if old in (None, "not_evaluable") or new in (None, "not_evaluable"):
        return InvariantComparison(name=name, result="NOT_VERIFIED", reason="not_evaluable")
    if old == "fail" and new == "fail":
        return InvariantComparison(name=name, result="STILL_FAILING")
    if old == "pass" and new == "fail":
        return InvariantComparison(name=name, result="REGRESSED")
    if old == "pass":
        return InvariantComparison(name=name, result="STILL_PASSING")
    reason = _not_verified_reason(baseline, candidate)
    if reason is not None:
        return InvariantComparison(name=name, result="NOT_VERIFIED", reason=reason)
    return InvariantComparison(name=name, result="FIXED")


def _performance_delta(baseline: Report, candidate: Report) -> dict[str, float]:
    if baseline.performance is None or candidate.performance is None:
        return {}
    old, new = baseline.performance.latency_ms, candidate.performance.latency_ms
    return {"p50_ms": round(new.p50 - old.p50, 3), "p95_ms": round(new.p95 - old.p95, 3)}


def not_comparable(baseline_run_id: str, reason: str) -> Comparison:
    return Comparison(
        baseline_run_id=baseline_run_id, status="NOT_COMPARABLE", not_comparable_reason=reason
    )


def _fix_verified(candidate: Report, invariants: Sequence[InvariantComparison]) -> bool:
    """An invariant can be FIXED while the retest fails in another way, such as unexpected 500s."""
    results = {item.result for item in invariants}
    return candidate.verdict == "PASS" and "FIXED" in results and not results & UNRESOLVED_CHANGES


def compare_reports(baseline: Report, candidate: Report) -> Comparison:
    for label, old, new in (
        ("scenario_id", baseline.scenario_id, candidate.scenario_id),
        ("config_hash", baseline.config.config_hash, candidate.config.config_hash),
        ("trials", baseline.config.trials, candidate.config.trials),
        ("serial_control", baseline.config.serial_control, candidate.config.serial_control),
    ):
        if old != new:
            return not_comparable(baseline.run_id, f"{label} differs")
    old_outcomes, new_outcomes = _invariant_outcomes(baseline), _invariant_outcomes(candidate)
    names = list(dict.fromkeys([*old_outcomes, *new_outcomes]))
    invariants = [
        _compare_invariant(name, old_outcomes.get(name), new_outcomes.get(name), baseline, candidate)
        for name in names
    ]
    return Comparison(
        baseline_run_id=baseline.run_id,
        status="COMPARABLE",
        baseline_build_id=baseline.target.build.build_id,
        candidate_build_id=candidate.target.build.build_id,
        invariants=invariants,
        fix_verified=_fix_verified(candidate, invariants),
        performance_delta=_performance_delta(baseline, candidate),
    )


# Rendering


def verdict_line(report: Report) -> str:
    done = report.completeness.trials_completed
    planned = report.completeness.trials_planned
    suffix = "complete" if report.completeness.status == "complete" else "incomplete"
    reasons = sorted({t.incomplete_reason for t in report.trials if t.incomplete_reason})
    detail = f": {', '.join(reasons)}" if reasons else ""
    return f"{report.verdict} ({done}/{planned} trials {suffix}{detail})"


def render_markdown(report: Report) -> str:
    lines = [f"# {report.scenario_id}: {verdict_line(report)}", "", report.verdict_reason, ""]
    lines += ["## Proven"]
    if report.violations:
        for violation in report.violations:
            label = violation.invariant or violation.type
            observed = ", ".join(f"{key}={value}" for key, value in sorted(violation.observed.items()))
            lines.append(f"- **{label}** in {violation.trials_violated} trials ({observed})")
    else:
        lines.append("- No violations.")
    lines += ["", "## Suspected", f"*{BLACK_BOX_NOTICE}*"]
    if report.hypotheses:
        for hypothesis in report.hypotheses:
            lines.append(
                f"- `{hypothesis.suspected_cause}` ({hypothesis.status}), "
                f"explains {', '.join(hypothesis.explains)}. {hypothesis.how_to_confirm}"
            )
    else:
        lines.append("- Nothing suspected.")
    if report.performance is not None:
        latency = report.performance.latency_ms
        lines += [
            "",
            "## Performance (doesn't affect the verdict)",
            f"p50 {latency.p50} ms, p95 {latency.p95} ms, "
            f"error rate {report.performance.error_rate}, "
            f"rejection rate {report.performance.rejection_rate}, "
            f"statuses {report.performance.status_counts}",
        ]
    if report.comparison is not None:
        lines += ["", f"## Compared with {report.comparison.baseline_run_id}"]
        if report.comparison.status == "NOT_COMPARABLE":
            lines.append(f"NOT_COMPARABLE: {report.comparison.not_comparable_reason}")
        for item in report.comparison.invariants:
            reason = f" ({item.reason})" if item.reason else ""
            lines.append(f"- {item.name}: {item.result}{reason}")
        if report.comparison.status == "COMPARABLE":
            lines.append(f"Fix verified: {'yes' if report.comparison.fix_verified else 'no'}")
    if report.disclosures:
        lines += ["", "## Disclosures", *[f"- {item}" for item in report.disclosures]]
    if report.reproduce is not None:
        lines += ["", f"Reproduce: `{report.reproduce.cli}`"]
    return "\n".join(lines) + "\n"


# Storage


def runs_dir(project_dir: Path) -> Path:
    path = project_dir / STORE_DIR / RUNS_DIR
    safety.contained(project_dir, path)
    path.mkdir(parents=True, exist_ok=True)
    safety.contained(project_dir, path)
    return path


def new_run_id() -> str:
    return f"run_{datetime.now(UTC):%Y%m%d_%H%M%S}_{os.urandom(2).hex()}"


def run_dir(project_dir: Path, run_id: str) -> Path:
    safety.validate_run_id(run_id)
    root = runs_dir(project_dir)
    return safety.contained(root, root / run_id)


@contextmanager
def run_lock(project_dir: Path) -> Iterator[None]:
    runs_dir(project_dir)
    path = safety.contained(project_dir, project_dir / STORE_DIR / "run.lock")
    with path.open("w", encoding="utf-8") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise FaultForgeError("busy", "another run is in progress", "Wait for it to finish.") from error
        yield


def _write_atomic(path: Path, content: str) -> None:
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as temp:
        temp.write(content)
    # NamedTemporaryFile creates 0600, and a different UID reads these artifacts.
    os.chmod(temp.name, ARTIFACT_MODE)
    os.replace(temp.name, path)


def describe_evidence(requests_path: Path) -> EvidenceFile:
    content = requests_path.read_bytes()
    return EvidenceFile(
        path=REQUESTS_NAME,
        count=content.count(b"\n"),
        sha256=hashlib.sha256(content).hexdigest(),
    )


def write_report(directory: Path, report: Report) -> None:
    _write_atomic(directory / REPORT_NAME, report.model_dump_json(indent=2) + "\n")
    _write_atomic(directory / SUMMARY_NAME, render_markdown(report))


def write_json(path: Path, value: object) -> None:
    _write_atomic(path, json.dumps(value, indent=2) + "\n")


def read_report(project_dir: Path, run_id: str) -> Report:
    try:
        path = run_dir(project_dir, run_id) / REPORT_NAME
        return Report.model_validate_json(path.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise FaultForgeError("run_not_found", f"no report for {run_id}") from error
    except (OSError, UnicodeDecodeError, ValidationError) as error:
        raise FaultForgeError("run_not_found", f"can't read the report for {run_id}: {error}") from error
