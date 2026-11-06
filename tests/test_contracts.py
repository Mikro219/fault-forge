import copy
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from faultforge.models import LoadConfig, ProjectConfig, Report, RequestRecord
from faultforge.schema import SCHEMA_PATH, render_report_schema

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).parent / "fixtures"


def load_report(name: str) -> dict:
    return json.loads((FIXTURES / f"report_{name}.json").read_text(encoding="utf-8"))


def assert_rejected(data: dict, message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        Report.model_validate(data)


@pytest.mark.parametrize(
    ("name", "verdict"),
    [("fail", "FAIL"), ("pass", "PASS"), ("inconclusive", "INCONCLUSIVE")],
)
def test_fixture_reports_validate(name, verdict):
    assert Report.model_validate(load_report(name)).verdict == verdict


def test_generated_schema_is_up_to_date():
    assert SCHEMA_PATH.read_text(encoding="utf-8") == render_report_schema(), (
        "run: uv run python -m faultforge.schema"
    )


def test_demo_config_validates():
    raw = yaml.safe_load((FIXTURES / "faultforge.yaml").read_text(encoding="utf-8"))
    config = ProjectConfig.model_validate(raw)
    types = {invariant.type for invariant in config.scenarios[0].invariants}
    assert {"liveness", "integrity"} <= types


def test_k6_lock_is_well_formed():
    lock = (ROOT / "k6.lock").read_text(encoding="utf-8")
    assert re.fullmatch(r"version=\d+\.\d+\.\d+\nsha256=[a-f0-9]{64}\n", lock)


def test_installed_k6_matches_pin():
    k6 = ROOT / ".tools" / "bin" / "k6"
    if not shutil.which(str(k6)):
        pytest.skip("k6 not installed; run scripts/install_k6.sh")
    version = re.search(r"version=(.+)", (ROOT / "k6.lock").read_text(encoding="utf-8")).group(1)
    output = subprocess.run([k6, "version"], capture_output=True, text=True, check=True).stdout
    assert f"v{version}" in output


# Verdict rules: a report can't claim more than its evidence supports


def test_pass_rejects_incomplete_trial():
    data = load_report("pass")
    data["trials"][1]["status"] = "incomplete"
    data["trials"][1]["incomplete_reason"] = "timeout"
    assert_rejected(data, "every planned trial")


def test_pass_rejects_failed_invariant():
    data = load_report("pass")
    data["trials"][0]["invariants"][0]["result"] = "fail"
    assert_rejected(data, "every invariant to pass")


def test_pass_rejects_not_evaluable_invariant():
    data = load_report("pass")
    data["trials"][0]["invariants"][0]["result"] = "not_evaluable"
    assert_rejected(data, "every invariant to pass")


def test_pass_rejects_unexpected_outcomes():
    data = load_report("pass")
    data["trials"][0]["client"]["unexpected"] = 1
    assert_rejected(data, "unexpected or transport")


def test_pass_rejects_proven_violations():
    data = load_report("pass")
    data["violations"] = copy.deepcopy(load_report("fail")["violations"])
    assert_rejected(data, "can't include proven violations")


@pytest.mark.parametrize(("planned", "completed"), [(20, 19), (0, 0)])
def test_pass_requires_every_planned_request_to_complete(planned, completed):
    data = load_report("pass")
    data["trials"][0]["planned_requests"] = planned
    data["trials"][0]["completed_requests"] = completed
    assert_rejected(data, "every planned request")


def test_pass_requires_every_planned_concurrent_trial():
    data = load_report("pass")
    data["trials"][0]["kind"] = "serial_control"
    assert_rejected(data, "as many concurrent trials")


def test_fix_verified_requires_a_passing_candidate():
    data = load_report("inconclusive")
    data["comparison"] = load_report("pass")["comparison"]
    assert_rejected(data, "verdict to be PASS")


@pytest.mark.parametrize("change", ["NOT_VERIFIED", "REGRESSED", "STILL_FAILING", "STILL_PASSING"])
def test_fix_verified_requires_fixed_and_nothing_unresolved(change):
    data = load_report("pass")
    reason = "not_evaluable" if change == "NOT_VERIFIED" else None
    data["comparison"]["invariants"] = [{"name": "x", "result": change, "reason": reason}]
    assert_rejected(data, "fix_verified requires a FIXED")


def test_fail_requires_a_violation():
    data = load_report("fail")
    data["violations"] = []
    data["hypotheses"] = []
    assert_rejected(data, "at least one proven violation")


def test_completed_concurrent_trial_requires_contention():
    data = load_report("pass")
    data["trials"][0]["peak_in_flight"] = 1
    assert_rejected(data, "below the required 10")


def test_serial_control_is_exempt_from_contention():
    data = load_report("pass")
    assert data["trials"][3]["kind"] == "serial_control"
    assert data["trials"][3]["peak_in_flight"] == 1
    Report.model_validate(data)


def test_insufficient_contention_can_be_reported_as_inconclusive():
    data = load_report("inconclusive")
    data["trials"][1]["incomplete_reason"] = "insufficient_contention"
    data["trials"][1]["peak_in_flight"] = 1
    assert Report.model_validate(data).verdict == "INCONCLUSIVE"


def test_inconclusive_needs_a_reason():
    data = load_report("pass")
    data["verdict"] = "INCONCLUSIVE"
    assert_rejected(data, "INCONCLUSIVE needs")


def test_error_means_no_trial_started():
    data = load_report("pass")
    data["verdict"] = "ERROR"
    assert_rejected(data, "no trial started")


def test_hypothesis_must_explain_a_real_violation():
    data = load_report("fail")
    data["hypotheses"][0]["explains"] = ["V99"]
    assert_rejected(data, "unknown violations")


def test_violation_samples_are_capped():
    data = load_report("fail")
    sample = data["violations"][0]["evidence"]["sample"][0]
    data["violations"][0]["evidence"]["sample"] = [sample] * 11
    assert_rejected(data, "at most 10")


def test_violations_are_always_proven():
    data = load_report("fail")
    data["violations"][0]["proven"] = False
    assert_rejected(data, "proven")


# Comparison and trial rules


def test_not_verified_requires_a_reason():
    data = load_report("pass")
    data["comparison"]["invariants"][0] = {
        "name": "x",
        "result": "NOT_VERIFIED",
        "reason": None,
    }
    assert_rejected(data, "reason is required")


def test_not_comparable_has_no_invariant_results():
    data = load_report("pass")
    data["comparison"]["status"] = "NOT_COMPARABLE"
    data["comparison"]["not_comparable_reason"] = "config_hash differs"
    assert_rejected(data, "NOT_COMPARABLE")


def test_reset_failed_is_a_valid_incomplete_reason():
    data = load_report("inconclusive")
    data["trials"][1]["incomplete_reason"] = "reset_failed"
    assert Report.model_validate(data).trials[1].incomplete_reason == "reset_failed"


def test_skipped_trial_must_be_budget_exhausted():
    data = load_report("inconclusive")
    data["trials"][2]["incomplete_reason"] = "timeout"
    assert_rejected(data, "budget_exhausted")


def test_completeness_counts_must_add_up():
    data = load_report("pass")
    data["completeness"]["trials_completed"] = 2
    assert_rejected(data, "don't add up")


def test_unknown_fields_are_rejected():
    data = load_report("pass")
    data["surprise"] = True
    assert_rejected(data, "surprise")


# Config rules


def test_mutating_scenario_requires_reset():
    raw = yaml.safe_load((FIXTURES / "faultforge.yaml").read_text(encoding="utf-8"))
    del raw["target"]["reset"]
    with pytest.raises(ValidationError, match="require target.reset"):
        ProjectConfig.model_validate(raw)


def test_duplicate_scenario_ids_are_rejected():
    raw = yaml.safe_load((FIXTURES / "faultforge.yaml").read_text(encoding="utf-8"))
    raw["scenarios"].append(copy.deepcopy(raw["scenarios"][0]))
    with pytest.raises(ValidationError, match="unique"):
        ProjectConfig.model_validate(raw)


def test_status_cannot_be_both_accepted_and_rejected():
    raw = yaml.safe_load((FIXTURES / "faultforge.yaml").read_text(encoding="utf-8"))
    raw["scenarios"][0]["outcomes"]["rejected_status"] = [200]
    with pytest.raises(ValidationError, match="both accepted and rejected"):
        ProjectConfig.model_validate(raw)


@pytest.mark.parametrize(("vus", "expected"), [(1, 2), (3, 2), (20, 10), (50, 25)])
def test_default_min_peak_in_flight(vus, expected):
    load = LoadConfig(vus=vus, iterations=vus, max_duration_s=5)
    assert load.effective_min_peak_in_flight == expected


def test_min_peak_in_flight_requires_real_overlap():
    with pytest.raises(ValidationError, match="greater than or equal to 2"):
        LoadConfig(vus=5, iterations=5, max_duration_s=5, min_peak_in_flight=1)


def test_iterations_must_cover_vus():
    with pytest.raises(ValidationError, match="at least vus"):
        LoadConfig(vus=5, iterations=4, max_duration_s=5)


def test_request_record_rejects_unknown_outcome():
    with pytest.raises(ValidationError):
        RequestRecord(
            trial=1, request_id="r", vu=1, iter=0, t_start=0, t_end=1,
            latency_ms=1, status=200, outcome="maybe",
        )
