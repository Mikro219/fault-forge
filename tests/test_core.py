"""M3 acceptance tests that need no running target: safety, budgets, expressions and verdict rules."""

import json
from pathlib import Path

import pytest
import yaml

from faultforge import config, expressions, hooks, k6_runner, lifecycle, oracle, report, safety
from faultforge.errors import FaultForgeError
from faultforge.limits import LIMITS
from faultforge.models import (
    ClientCounts,
    Completeness,
    InvariantConfig,
    Report,
    RequestRecord,
    TrialResult,
)

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).parent / "fixtures"
DEMO_CONFIG = ROOT / "demo" / "ticket-api" / "faultforge.yaml"


def load_report(name: str) -> dict:
    return json.loads((FIXTURES / f"report_{name}.json").read_text(encoding="utf-8"))


def error_class(function, *args) -> str:
    with pytest.raises(FaultForgeError) as caught:
        function(*args)
    return caught.value.error_class


# Target and path safety


@pytest.mark.parametrize(
    "origin",
    [
        "http://example.com",
        "http://example.com:80",
        "http://localhost.example.com:8000",
        "http://user@127.0.0.1:8000",
        "http://user:pw@127.0.0.1:8000",
        "http://127.0.0.1@evil.com:8000",
        "http://127.1:8000",
        "http://0x7f.1:8000",
        "http://2130706433:8000",
        "http://[::1]:8000",
        "http://0.0.0.0:8000",
        "http://127.0.0.2:8000",
        "https://127.0.0.1",
        "https://127.0.0.1:8000",
        "ftp://127.0.0.1:21",
        "http://127.0.0.1",
        "http://127.0.0.1:0",
        "http://127.0.0.1:99999",
        "http://127.0.0.1:8000/path",
        "http://127.0.0.1:8000?x=1",
        "http://127.0.0.1:8000#x",
        "http://127.0.0.1:8000 ",
        "http://127.0.0.1:80\n00",
    ],
)
def test_unsafe_origins_are_rejected(origin):
    assert error_class(safety.validate_origin, origin) == "unsafe_target"


def test_safe_origins_are_canonical():
    assert safety.validate_origin("http://127.0.0.1:8000") == "http://127.0.0.1:8000"
    assert safety.validate_origin("http://localhost:8000") == "http://127.0.0.1:8000"


@pytest.mark.parametrize(
    "path",
    [
        "//evil.com/x",
        "http://evil.com",
        "evil",
        "",
        "/a/../b",
        "/a%2f..",
        "/a%2F..",
        "/a%2e%2e/b",
        "/a%5cb",
        "/a\\b",
        "/@evil",
        "/a\x01b",
        "/a\nb",
        "/a#b",
        "/${VU}",
        "/x?next=http://evil.com",
        "/" + "a" * 256,
    ],
)
def test_unsafe_paths_are_rejected(path):
    assert error_class(safety.validate_path, path) == "config_invalid"


def test_plain_paths_are_accepted():
    assert safety.build_url("http://127.0.0.1:8000", "/reserve?x=1") == "http://127.0.0.1:8000/reserve?x=1"


def test_project_outside_roots_is_rejected(tmp_path):
    root = tmp_path / "allowed"
    other = tmp_path / "other"
    root.mkdir()
    other.mkdir()
    assert error_class(safety.resolve_project_dir, other, [root]) == "config_invalid"
    assert safety.resolve_project_dir(root, [root]) == root.resolve()


def test_symlink_cannot_escape_roots(tmp_path):
    root = tmp_path / "allowed"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    (root / "link").symlink_to(outside)
    assert error_class(safety.resolve_project_dir, root / "link", [root]) == "config_invalid"


@pytest.mark.parametrize("run_id", ["../../etc", "run_1", "run_20261106_071900_ABCD", "", "run_20261106_071900_ab12/.."])
def test_bad_run_ids_are_rejected(run_id, tmp_path):
    assert error_class(report.read_report, tmp_path, run_id) == "run_not_found"


def test_runs_directory_symlinked_outside_is_rejected(tmp_path):
    project = tmp_path / "project"
    outside = tmp_path / "outside"
    (project / ".faultforge").mkdir(parents=True)
    outside.mkdir()
    (project / ".faultforge" / "runs").symlink_to(outside)
    assert error_class(report.runs_dir, project) == "config_invalid"


# Config


def demo_config() -> dict:
    return yaml.safe_load(DEMO_CONFIG.read_text(encoding="utf-8"))


def test_demo_config_loads():
    project = config.parse_config(DEMO_CONFIG.read_text(encoding="utf-8"))
    assert project.scenarios[0].id == "last-ticket-rush"


@pytest.mark.parametrize(
    ("edit", "expected"),
    [
        (lambda c: c["target"].update(origin="http://example.com:80"), "unsafe_target"),
        (lambda c: c["scenarios"][0]["request"].update(path="//evil.com/x"), "config_invalid"),
        (lambda c: c["scenarios"][0]["request"].update(path="/a/${VU}"), "config_invalid"),
        (lambda c: c["target"].update(state="http://evil.com/state"), "config_invalid"),
        (lambda c: c["target"]["reset"].update(path="/../reset"), "config_invalid"),
        (lambda c: c["scenarios"][0].update(script="../../evil"), "config_invalid"),
        (lambda c: c["scenarios"][0]["invariants"][0].update(check="__import__('os')"), "config_invalid"),
        (lambda c: c["scenarios"][0]["outcomes"].update(capture_field="a|b"), "config_invalid"),
        (lambda c: c["scenarios"][0]["request"].update(json={"x": "y" * 20_000}), "config_invalid"),
        (lambda c: c.update(command="rm -rf /"), "config_invalid"),
    ],
)
def test_unsafe_config_is_rejected(edit, expected):
    raw = demo_config()
    edit(raw)
    assert error_class(config.parse_config, yaml.safe_dump(raw)) == expected


def test_invalid_yaml_is_config_invalid():
    assert error_class(config.parse_config, "a: [") == "config_invalid"


def test_yaml_tags_cannot_construct_objects():
    assert error_class(config.parse_config, "!!python/object/apply:os.system ['true']") == "config_invalid"


def demo_text_with_body(body: str) -> str:
    text = DEMO_CONFIG.read_text(encoding="utf-8")
    old = 'json: { item_id: 1, buyer: "buyer-${VU}-${ITER}" }'
    assert old in text
    return text.replace(old, f"json: {body}")


ALIAS_BOMB = "{" + ", ".join(
    f"k{level}: &a{level} [" + ",".join(["*a%d" % (level - 1)] * 9 if level else ["1"] * 9) + "]"
    for level in range(11)
) + "}"


@pytest.mark.parametrize(
    "text",
    [
        demo_text_with_body("{ when: 2020-01-01 }"),
        demo_text_with_body("{ when: 2020-13-45 }"),
        demo_text_with_body("&r { self: *r }"),
        demo_text_with_body(ALIAS_BOMB),
        demo_text_with_body("{ size: " + "9" * 5000 + " }"),
        demo_text_with_body("{ score: .nan }"),
        demo_text_with_body("{ score: .inf }"),
        demo_text_with_body("[" * 20_000 + "]" * 20_000),
        DEMO_CONFIG.read_text(encoding="utf-8").replace("json: { stock: 1 }", "json: { stock: .nan }"),
        DEMO_CONFIG.read_text(encoding="utf-8").replace(
            "accepted_status: [200, 201]", "accepted_status: [" + ",".join(["200"] * 55_000) + "]"
        ),
    ],
    ids=["date", "invalid-date", "recursive-alias", "alias-bomb", "huge-int", "nan", "inf", "deep", "nan-reset", "statuses"],
)
def test_hostile_yaml_is_config_invalid(text):
    assert text != DEMO_CONFIG.read_text(encoding="utf-8")
    assert error_class(config.parse_config, text) == "config_invalid"


@pytest.mark.parametrize("field", ["accepted_status", "rejected_status"])
def test_status_lists_are_capped(field):
    raw = demo_config()
    raw["scenarios"][0]["outcomes"].update(accepted_status=[200], rejected_status=[409])
    raw["scenarios"][0]["outcomes"][field] = list(range(300, 332))
    config.parse_config(yaml.safe_dump(raw))
    raw["scenarios"][0]["outcomes"][field] = list(range(300, 333))
    assert error_class(config.parse_config, yaml.safe_dump(raw)) == "config_invalid"


def test_config_cannot_allow_a_no_overlap_pass():
    raw = demo_config()
    raw["scenarios"][0]["load"]["min_peak_in_flight"] = 1
    assert error_class(config.parse_config, yaml.safe_dump(raw)) == "config_invalid"


def test_duplicate_invariant_names_are_rejected():
    raw = demo_config()
    invariants = raw["scenarios"][0]["invariants"]
    invariants[1]["name"] = invariants[0]["name"]
    assert error_class(config.parse_config, yaml.safe_dump(raw)) == "config_invalid"


def capacity_config(check: str) -> str:
    raw = demo_config()
    raw["scenarios"][0]["invariants"][0]["check"] = check
    return yaml.safe_dump(raw)


@pytest.mark.parametrize(
    "check",
    [
        "client.accepted >= before.stock",
        "client.accepted == before.stock",
        "client.accepted <= after.stock",
        "client.accepted <= before.stock + after.stock",
        "client.accepted <= client.total",
        "before.stock >= client.accepted",
        "client.accepted + 1 <= before.stock",
        "after.stock <= before.stock",
        "client.accepted <= before.stock <= 5",
        "client.accepted <= before.stock and client.total <= 5",
    ],
)
def test_capacity_checks_must_be_client_upper_bounds(check):
    assert error_class(config.parse_config, capacity_config(check)) == "config_invalid"


@pytest.mark.parametrize(
    "check", ["client.accepted <= before.stock", "client.total < before.stock * 2 + 1", "client.accepted <= 5"]
)
def test_client_upper_bounds_are_valid_capacity_checks(check):
    config.parse_config(capacity_config(check))


def test_other_invariant_types_can_use_any_form():
    raw = demo_config()
    raw["scenarios"][0]["invariants"][1]["check"] = "client.accepted >= after.stock"
    config.parse_config(yaml.safe_dump(raw))


# Hook responses


def test_snapshot_only_keeps_safe_state_fields():
    snapshot = hooks.parse_snapshot(
        {
            "stock": 1, "orders_2": -2, "max": 2**63 - 1, "huge": 2**63, "tiny": -(2**63),
            "Upper": 1, "2nd": 1, "has-dash": 1, "new\nline": 1, "\ud800": 1, "flag": True, "ratio": 0.5,
        }
    )
    assert snapshot.values == {"stock": 1, "orders_2": -2, "max": 2**63 - 1}


@pytest.mark.parametrize("value", ["\ud800", "", "a\nb", "caf\u00e9", "\x7f", "a" * 129, 5, None])
def test_unsafe_build_values_are_treated_as_absent(value):
    snapshot = hooks.parse_snapshot({"build": {"build_id": value, "instance_id": "ok ~"}})
    assert snapshot.build.build_id is None
    assert snapshot.build.instance_id == "ok ~"


def test_in_flight_outside_the_integer_range_is_treated_as_absent():
    assert hooks.parse_snapshot({"in_flight": 2**63}).in_flight is None


# Run artifacts


def test_output_cut_mid_character_does_not_crash(tmp_path):
    tags = {
        "ff_trial": "1", "ff_request_id": "t1-0", "ff_vu": "1", "ff_iter": "0", "ff_t_start": "1",
        "ff_t_end": "2", "ff_status": "200", "ff_outcome": "accepted", "ff_captured": "\u20ac" * 10,
    }
    line = json.dumps(
        {"type": "Point", "metric": k6_runner.EVIDENCE_METRIC, "data": {"tags": tags, "value": 1.0}},
        ensure_ascii=False,
    ).encode()
    path = tmp_path / "raw.json"
    path.write_bytes(line + b"\n" + line[: line.index("\u20ac".encode()) + 1])
    records = k6_runner.read_records(path)
    assert [record.request_id for record in records] == ["t1-0"]


def test_atomic_writes_are_world_readable(tmp_path):
    path = tmp_path / "artifact"
    report.write_json(path, {})
    assert path.stat().st_mode & 0o777 == 0o644


# Budgets


def scenario():
    return config.parse_config(DEMO_CONFIG.read_text(encoding="utf-8")).scenarios[0]


def plan_for(**options):
    return lifecycle.resolve_plan(scenario(), lifecycle.RunOptions(**options), None, LIMITS)


def test_over_budget_requests_are_rejected_with_a_suggestion():
    with pytest.raises(FaultForgeError) as caught:
        lifecycle.admit(plan_for(trials=5, iterations=500, serial_control=False), LIMITS)
    assert caught.value.error_class == "budget_exceeded"
    suggested = caught.value.suggested_plan
    assert suggested["trials"] * suggested["iterations"] <= LIMITS.max_requests_total


def test_request_budget_is_enforced_on_its_own():
    plan = plan_for(trials=5, iterations=500, max_duration_s=1, serial_control=False)
    with pytest.raises(FaultForgeError) as caught:
        lifecycle.admit(plan, LIMITS)
    assert caught.value.error_class == "budget_exceeded"
    assert caught.value.message == "2500 requests exceeds the limit of 2000"


def test_over_budget_wall_time_is_rejected():
    plan = plan_for(trials=3, max_duration_s=20, serial_control=False)
    assert error_class(lifecycle.admit, plan, LIMITS) == "budget_exceeded"


def test_too_many_trials_are_rejected_not_reduced():
    plan = plan_for(trials=6, max_duration_s=1)
    assert plan.trials == 6
    assert error_class(lifecycle.admit, plan, LIMITS) == "budget_exceeded"


def test_serial_control_counts_as_a_trial():
    assert error_class(lifecycle.admit, plan_for(trials=5, max_duration_s=1), LIMITS) == "budget_exceeded"
    lifecycle.admit(plan_for(trials=4, max_duration_s=1), LIMITS)


def test_default_demo_plan_is_admitted():
    lifecycle.admit(plan_for(), LIMITS)


def test_load_is_clamped_and_reported():
    plan = plan_for(vus=500, iterations=9000, max_duration_s=999)
    assert (plan.load.vus, plan.load.iterations, plan.load.max_duration_s) == (50, 500, 20)
    assert {clamp.field for clamp in plan.clamps} == {"vus", "iterations", "max_duration_s"}


@pytest.mark.parametrize("field", ["vus", "iterations", "max_duration_s"])
def test_non_positive_load_is_rejected(field):
    with pytest.raises(FaultForgeError) as caught:
        plan_for(**{field: 0})
    assert caught.value.error_class == "config_invalid"


def test_zero_trials_are_rejected():
    assert error_class(lifecycle.admit, plan_for(trials=0), LIMITS) == "config_invalid"


def test_lowering_vus_keeps_requiring_overlap():
    plan = plan_for(vus=1)
    assert plan.load.effective_min_peak_in_flight == 2


def test_limits_cannot_be_raised_by_config():
    raw = demo_config()
    raw["scenarios"][0]["load"].update(vus=9999, iterations=9999, max_duration_s=9999, min_peak_in_flight=None)
    parsed = config.parse_config(yaml.safe_dump(raw)).scenarios[0]
    plan = lifecycle.resolve_plan(parsed, lifecycle.RunOptions(), None, LIMITS)
    assert (plan.load.vus, plan.load.iterations, plan.load.max_duration_s) == (50, 500, 20)


# Expression whitelist


@pytest.mark.parametrize(
    "source",
    [
        "__import__('os')",
        "f()",
        "a[0]",
        "before.__class__",
        "before.__class__ == 1",
        "before.stock.real == 1",
        "unknown.stock == 1",
        "stock == 1",
        "client.secret == 1",
        "(lambda: 1)() == 1",
        "[x for x in before] == 1",
        "before.stock if 1 else 2",
        "'a' == 'a'",
        "before.stock ** 99 == 1",
        "before.stock",
        "1 + 1",
        "before.stock in [1]",
        "before.stock is 1",
        "1.5 == before.stock",
        "True == True",
        "1 == 1",
        "1 < 2 < 3",
        "1 + 1 == 2",
        "client.accepted <= 1 or 1",
        "client.accepted <= 1 or 1 == 1",
        "client.accepted <= 1 and before.stock",
        "not 1",
        "not before.stock",
        "not 1 == 1",
        "(client.accepted > 1) + 1 > 0",
        "client.accepted <= (not before.stock)",
        "(" * 400 + "1 == 1" + ")" * 400,
        "a = 1",
        "x" * 600,
    ],
)
def test_disallowed_expressions_are_rejected(source):
    with pytest.raises(expressions.ExpressionError):
        expressions.parse(source)


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("client.accepted <= before.stock", False),
        ("before.stock - after.stock == client.accepted", False),
        ("after.stock >= 0 and client.accepted > 1", True),
        ("after.stock > 5 or client.accepted == 3", True),
        ("not after.stock > 5", True),
        ("0 <= after.stock <= before.stock", True),
        ("client.accepted // 2 == 1", True),
        ("client.accepted * 2 + 1 == 7", True),
    ],
)
def test_expressions_evaluate(source, expected):
    values = {"client.accepted": 3, "before.stock": 1, "after.stock": 0}
    assert expressions.parse(source).evaluate(values) is expected


def test_missing_value_is_not_evaluable():
    with pytest.raises(expressions.NotEvaluable):
        expressions.parse("after.stock == 0").evaluate({})


def test_division_by_zero_is_not_evaluable():
    with pytest.raises(expressions.NotEvaluable):
        expressions.parse("client.accepted // after.stock == 0").evaluate({"client.accepted": 1, "after.stock": 0})


# Evaluating invariants


INVARIANTS = [
    InvariantConfig(name="cap", type="capacity", check="client.accepted <= before.stock"),
    InvariantConfig(name="acct", type="accounting", check="after.stock >= 0"),
]
COUNTS = ClientCounts(accepted=5, rejected=0, unexpected=0, transport_error=0)


def test_completed_trial_evaluates_everything():
    results = oracle.evaluate_invariants(INVARIANTS, COUNTS, {"stock": 1}, {"stock": 0}, True)
    assert [result.result for result in results] == ["fail", "pass"]


def test_incomplete_trial_only_evaluates_capacity():
    results = oracle.evaluate_invariants(INVARIANTS, COUNTS, {"stock": 1}, {"stock": 0}, False)
    assert [result.result for result in results] == ["fail", "not_evaluable"]


def test_missing_state_is_not_evaluable_rather_than_passing():
    results = oracle.evaluate_invariants(INVARIANTS, COUNTS, None, None, True)
    assert [result.result for result in results] == ["not_evaluable", "not_evaluable"]


# Verdict rules


def trial(number=1, kind="concurrent", status="completed", reason=None, results=("pass",), accepted=1):
    return TrialResult(
        trial=number, kind=kind, status=status, incomplete_reason=reason,
        planned_requests=20, completed_requests=20, peak_in_flight=20,
        client=ClientCounts(accepted=accepted, rejected=0, unexpected=0, transport_error=0),
        invariants=[{"name": "cap", "result": result} for result in results],
    )


def completeness(done=1, incomplete=0, skipped=0):
    planned = done + incomplete + skipped
    return Completeness(
        status="complete" if done == planned else "incomplete",
        trials_planned=planned, trials_completed=done, trials_incomplete=incomplete,
        trials_skipped=skipped, serial_control="not_requested",
    )


def scenario_with_capacity():
    return scenario().model_copy(update={"invariants": [INVARIANTS[0]]})


def test_incomplete_trial_with_capacity_violation_is_fail():
    trials = [trial(status="incomplete", reason="timeout", results=("fail",), accepted=5)]
    violations = oracle.find_violations(scenario_with_capacity(), trials, [])
    verdict, _ = oracle.decide_verdict(trials, completeness(done=0, incomplete=1), violations)
    assert verdict == "FAIL"
    assert violations[0].type == "invariant.capacity_exceeded"


def test_incomplete_without_violation_is_inconclusive_never_pass():
    trials = [trial(status="incomplete", reason="timeout", results=("not_evaluable",))]
    violations = oracle.find_violations(scenario_with_capacity(), trials, [])
    assert oracle.decide_verdict(trials, completeness(done=0, incomplete=1), violations)[0] == "INCONCLUSIVE"


def test_not_evaluable_on_complete_trials_is_inconclusive():
    trials = [trial(results=("not_evaluable",))]
    assert oracle.decide_verdict(trials, completeness(), [])[0] == "INCONCLUSIVE"


def test_completed_plus_skipped_is_inconclusive_with_nothing_not_evaluable():
    skipped = TrialResult(
        trial=2, kind="concurrent", status="skipped", incomplete_reason="budget_exhausted",
        planned_requests=20, completed_requests=0, client=COUNTS.model_copy(update={"accepted": 0}),
    )
    verdict, reason = oracle.decide_verdict([trial(), skipped], completeness(done=1, skipped=1), [])
    assert verdict == "INCONCLUSIVE"
    assert "1/2 trials completed" in reason


def test_all_skipped_is_error():
    skipped = TrialResult(
        trial=1, kind="concurrent", status="skipped", incomplete_reason="budget_exhausted",
        planned_requests=20, completed_requests=0, client=COUNTS.model_copy(update={"accepted": 0}),
    )
    assert oracle.decide_verdict([skipped], completeness(done=0, skipped=1), [])[0] == "ERROR"


def test_transport_errors_on_an_incomplete_trial_are_not_proof():
    broken = trial(status="incomplete", reason="target_restarted", results=("not_evaluable",)).model_copy(
        update={"client": ClientCounts(accepted=0, rejected=0, unexpected=0, transport_error=20)}
    )
    assert oracle.find_violations(scenario_with_capacity(), [broken], []) == []


def test_unexpected_responses_on_a_complete_trial_are_proof():
    broken = trial().model_copy(
        update={"client": ClientCounts(accepted=1, rejected=0, unexpected=2, transport_error=0)}
    )
    violations = oracle.find_violations(scenario_with_capacity(), [broken], [])
    assert [violation.type for violation in violations] == ["response.unexpected_status"]


def test_hypothesis_is_suspected_without_serial_control():
    trials = [trial(results=("fail",), accepted=5)]
    records = [record(i, trial_number=1) for i in range(5)]
    violations = oracle.find_violations(scenario_with_capacity(), trials, records)
    hypotheses = oracle.find_hypotheses(violations, trials, records)
    assert [(h.suspected_cause, h.status) for h in hypotheses] == [("check_then_act_race", "suspected")]


def test_hypothesis_is_supported_only_when_serial_control_held():
    records = [record(i, trial_number=1) for i in range(5)]
    held = [trial(results=("fail",), accepted=5), trial(2, "serial_control", results=("pass",))]
    violations = oracle.find_violations(scenario_with_capacity(), held, records)
    assert oracle.find_hypotheses(violations, held, records)[0].status == "supported"

    failed_too = [trial(results=("fail",), accepted=5), trial(2, "serial_control", results=("fail",), accepted=5)]
    violations = oracle.find_violations(scenario_with_capacity(), failed_too, records)
    assert violations[0].facts["serial_control_violated"] is True
    assert oracle.find_hypotheses(violations, failed_too, records)[0].status == "suspected"


def test_serial_only_violation_reports_its_own_denominator():
    trials = [trial(results=("pass",)), trial(2, "serial_control", results=("fail",), accepted=5)]
    violations = oracle.find_violations(scenario_with_capacity(), trials, [])
    assert violations[0].trials_violated == "1/1"


# Report size and evidence


def record(index: int, trial_number: int = 1, outcome: str = "accepted") -> RequestRecord:
    return RequestRecord(
        trial=trial_number, request_id=f"t{trial_number}-{index}", vu=index + 1, iter=0,
        t_start=1000.0 + index, t_end=2000.0 + index, latency_ms=1000.0, status=200,
        outcome=outcome, captured=str(index),
    )


def test_large_run_stays_within_the_report_size_target():
    records = [record(i, trial_number=1 + i % 4) for i in range(2000)]
    data = load_report("fail")
    parsed = Report.model_validate(data)
    violations = oracle.find_violations(scenario(), parsed.trials, records)
    assert violations
    parsed.violations = violations
    parsed.hypotheses = oracle.find_hypotheses(violations, parsed.trials, records)
    parsed.performance = report.build_performance(parsed.trials, records, [1.0, 2.0, 3.0])
    assert len(parsed.model_dump_json()) <= 50_000
    assert all(len(violation.evidence.sample) <= 10 for violation in violations)


def test_percentile_uses_linear_interpolation():
    assert report.percentile([1, 2, 3, 4], 0.5) == 2.5
    assert report.percentile([10], 0.95) == 10
    assert report.percentile([0, 100], 0.95) == 95


# Comparison rules


def pair():
    return Report.model_validate(load_report("fail")), Report.model_validate(load_report("pass"))


def results_by_name(comparison) -> dict:
    return {item.name: item for item in comparison.invariants}


CAPACITY = "No more tickets confirmed than were in stock"
ACCOUNTING = "Stock accounting balances"


def test_bug_then_fix_is_fixed():
    baseline, candidate = pair()
    comparison = report.compare_reports(baseline, candidate)
    assert comparison.status == "COMPARABLE"
    results = results_by_name(comparison)
    assert results[CAPACITY].result == "FIXED"
    assert results[ACCOUNTING].result == "FIXED"
    assert results["Stock never negative"].result == "STILL_PASSING"


def test_unchanged_build_is_not_verified():
    baseline, candidate = pair()
    candidate.target.build.build_id = baseline.target.build.build_id
    result = results_by_name(report.compare_reports(baseline, candidate))[CAPACITY]
    assert (result.result, result.reason) == ("NOT_VERIFIED", "build_unchanged")


def test_unknown_build_is_not_verified():
    baseline, candidate = pair()
    candidate.target.build.build_id = None
    result = results_by_name(report.compare_reports(baseline, candidate))[CAPACITY]
    assert (result.result, result.reason) == ("NOT_VERIFIED", "build_unknown")


def test_unstable_build_is_not_verified():
    baseline, candidate = pair()
    candidate.target.build_stable_during_run = False
    result = results_by_name(report.compare_reports(baseline, candidate))[CAPACITY]
    assert (result.result, result.reason) == ("NOT_VERIFIED", "build_unknown")


@pytest.mark.parametrize("stale", [True, None])
def test_stale_or_unknown_source_is_not_verified(stale):
    baseline, candidate = pair()
    candidate.target.build.stale_source = stale
    result = results_by_name(report.compare_reports(baseline, candidate))[CAPACITY]
    assert (result.result, result.reason) == ("NOT_VERIFIED", "stale_source")


def test_stale_source_wins_over_an_unchanged_build():
    baseline, candidate = pair()
    candidate.target.build.build_id = baseline.target.build.build_id
    candidate.target.build.stale_source = True
    result = results_by_name(report.compare_reports(baseline, candidate))[CAPACITY]
    assert (result.result, result.reason) == ("NOT_VERIFIED", "stale_source")


def test_unknown_source_does_not_hide_an_unchanged_build():
    baseline, candidate = pair()
    candidate.target.build.build_id = baseline.target.build.build_id
    candidate.target.build.stale_source = None
    result = results_by_name(report.compare_reports(baseline, candidate))[CAPACITY]
    assert (result.result, result.reason) == ("NOT_VERIFIED", "build_unchanged")


def test_incomplete_candidate_is_not_verified():
    baseline, candidate = pair()
    candidate.completeness.status = "incomplete"
    result = results_by_name(report.compare_reports(baseline, candidate))[CAPACITY]
    assert (result.result, result.reason) == ("NOT_VERIFIED", "trials_incomplete")


@pytest.mark.parametrize("side", ["baseline", "candidate"])
def test_not_evaluable_on_either_side_is_not_verified(side):
    baseline, candidate = pair()
    chosen = baseline if side == "baseline" else candidate
    for result in chosen.trials[0].invariants:
        if result.name == "Stock never negative":
            result.result = "not_evaluable"
    item = results_by_name(report.compare_reports(baseline, candidate))["Stock never negative"]
    assert (item.result, item.reason) == ("NOT_VERIFIED", "not_evaluable")


def test_regression_and_still_failing():
    baseline, candidate = pair()
    for result in candidate.trials[1].invariants:
        if result.name in (CAPACITY, "Stock never negative"):
            result.result = "fail"
    results = results_by_name(report.compare_reports(baseline, candidate))
    assert results[CAPACITY].result == "STILL_FAILING"
    assert results["Stock never negative"].result == "REGRESSED"


def test_one_failing_trial_means_the_invariant_failed():
    baseline, candidate = pair()
    candidate.trials[3].invariants[0].result = "fail"
    assert results_by_name(report.compare_reports(baseline, candidate))[CAPACITY].result == "STILL_FAILING"


def test_invariant_only_evaluated_in_some_trials_is_not_evaluable():
    baseline, candidate = pair()
    for later in candidate.trials[1:]:
        later.invariants = []
    item = results_by_name(report.compare_reports(baseline, candidate))["Stock never negative"]
    assert (item.result, item.reason) == ("NOT_VERIFIED", "not_evaluable")


def test_failure_counts_even_when_other_trials_have_no_result():
    baseline, candidate = pair()
    candidate.trials[0].invariants = []
    for result in candidate.trials[1].invariants:
        if result.name == "Stock never negative":
            result.result = "fail"
    assert results_by_name(report.compare_reports(baseline, candidate))["Stock never negative"].result == "REGRESSED"


def test_fix_is_verified_only_for_a_clean_candidate():
    baseline, candidate = pair()
    assert report.compare_reports(baseline, candidate).fix_verified is True


def test_fix_is_not_verified_when_the_candidate_did_not_pass():
    baseline, candidate = pair()
    candidate.verdict = "FAIL"
    comparison = report.compare_reports(baseline, candidate)
    assert results_by_name(comparison)[CAPACITY].result == "FIXED"
    assert comparison.fix_verified is False


def test_fix_is_not_verified_when_nothing_was_failing():
    candidate = pair()[1]
    comparison = report.compare_reports(candidate, candidate)
    assert {item.result for item in comparison.invariants} == {"STILL_PASSING"}
    assert comparison.fix_verified is False


@pytest.mark.parametrize(("name", "outcome"), [(ACCOUNTING, "fail"), ("Stock never negative", "fail"), ("Stock never negative", "not_evaluable")])
def test_fix_is_not_verified_when_any_invariant_is_unresolved(name, outcome):
    baseline, candidate = pair()
    for result in candidate.trials[0].invariants:
        if result.name == name:
            result.result = outcome
    comparison = report.compare_reports(baseline, candidate)
    assert results_by_name(comparison)[CAPACITY].result == "FIXED"
    assert comparison.fix_verified is False


@pytest.mark.parametrize(
    "edit",
    [
        lambda r: setattr(r, "scenario_id", "other"),
        lambda r: setattr(r.config, "config_hash", "sha256:" + "0" * 64),
        lambda r: setattr(r.config, "trials", 1),
        lambda r: setattr(r.config, "serial_control", False),
    ],
)
def test_mismatched_runs_are_not_comparable(edit):
    baseline, candidate = pair()
    edit(candidate)
    comparison = report.compare_reports(baseline, candidate)
    assert comparison.status == "NOT_COMPARABLE"
    assert comparison.invariants == []


# Rendering


def test_markdown_separates_proven_from_suspected_and_shows_completeness():
    text = report.render_markdown(Report.model_validate(load_report("fail")))
    assert "FAIL (3/3 trials complete)" in text.splitlines()[0]
    assert text.index("## Proven") < text.index("## Suspected")
    assert report.BLACK_BOX_NOTICE in text
    assert len(text.splitlines()) <= 60


def test_incomplete_reasons_appear_next_to_the_verdict():
    line = report.verdict_line(Report.model_validate(load_report("inconclusive")))
    assert line.startswith("INCONCLUSIVE (1/3 trials incomplete: ")
    assert "timeout" in line


def test_markdown_is_short_with_every_section_present():
    data = Report.model_validate(load_report("pass"))
    assert len(report.render_markdown(data).splitlines()) <= 60


def test_hard_limits_match_the_plan():
    assert (LIMITS.max_vus, LIMITS.max_iterations_per_trial, LIMITS.max_trial_duration_s) == (50, 500, 20)
    assert (LIMITS.max_trials, LIMITS.max_requests_total, LIMITS.max_wall_s_total) == (5, 2000, 60)
