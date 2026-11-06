"""M3 end-to-end tests: real k6, real targets, and a scriptable target for failure paths."""

import gzip
import hashlib
import json
import os
import signal
import stat
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
import yaml
from test_ticket_api import BUGGY, NO_PAYMENT, REFERENCE_FIX, REJECT_ALL, SLOPPY_FIX, TICKET_API, Server, free_port

from faultforge import k6_runner, lifecycle, report
from faultforge.cli import main as cli_main
from faultforge.errors import FaultForgeError
from faultforge.hooks import HookError, TargetHooks
from faultforge.k6_runner import EngineResult
from faultforge.lifecycle import NotComparable, RunOptions, run_scenario
from faultforge.limits import LIMITS, Limits
from faultforge.models import TargetConfig

SCENARIO = "last-ticket-rush"
CAPACITY = "No more tickets confirmed than were in stock"
ACCOUNTING = "Stock accounting balances"
FAST = RunOptions(trials=1, serial_control=False)
RESTART_NOTE = "Restart detection unavailable: the target doesn't report an instance_id."


def write_project(path: Path, origin: str, edit=None) -> Path:
    raw = yaml.safe_load((TICKET_API / "faultforge.yaml").read_text(encoding="utf-8"))
    raw["target"]["origin"] = origin
    if edit is not None:
        edit(raw)
    path.mkdir(parents=True, exist_ok=True)
    (path / "faultforge.yaml").write_text(yaml.safe_dump(raw), encoding="utf-8")
    return path


def run(project: Path, options=RunOptions(), **kwargs):
    return run_scenario(project, SCENARIO, options, roots=[project], **kwargs)


class TargetHarness:
    """Starts one app at a time on a stable port, as the same project's target."""

    def __init__(self, tmp_path: Path):
        self.tmp_path = tmp_path
        self.port = free_port()
        self.server: Server | None = None
        self.count = 0

    def start(self, app_dir: Path) -> None:
        self.stop()
        self.count += 1
        server = Server(app_dir, self.tmp_path / f"db{self.count}.db")
        server.port = self.port
        server.url = f"http://127.0.0.1:{self.port}"
        self.server = server.start()

    def stop(self) -> None:
        if self.server is not None:
            self.server.stop()
            self.server = None


@pytest.fixture
def target(tmp_path):
    harness = TargetHarness(tmp_path)
    yield harness
    harness.stop()


@pytest.fixture
def project(tmp_path, target):
    return write_project(tmp_path / "project", f"http://127.0.0.1:{target.port}")


def violation_types(result) -> set[str]:
    return {violation.type for violation in result.violations}


def causes(result) -> dict[str, str]:
    return {hypothesis.suspected_cause: hypothesis.status for hypothesis in result.hypotheses}


# The demo's real verdicts


def test_buggy_code_fails_with_proven_violations(project, target):
    target.start(BUGGY)
    result = run(project, RunOptions(trials=3, serial_control=True))
    assert result.verdict == "FAIL"
    assert violation_types(result) == {"invariant.capacity_exceeded", "invariant.accounting_mismatch"}
    assert all(violation.proven and violation.trials_violated == "3/3" for violation in result.violations)
    assert causes(result) == {"check_then_act_race": "supported", "lost_update": "supported"}
    assert result.completeness.status == "complete"
    assert result.budget.requests_sent == 80
    assert result.trials[-1].kind == "serial_control"
    assert all(inv.result == "pass" for inv in result.trials[-1].invariants)


def test_without_serial_control_causes_stay_suspected(project, target):
    target.start(BUGGY)
    result = run(project, FAST)
    assert causes(result) == {"check_then_act_race": "suspected", "lost_update": "suspected"}


def test_reference_fix_passes(project, target):
    target.start(REFERENCE_FIX)
    result = run(project, RunOptions(trials=3, serial_control=True))
    assert result.verdict == "PASS"
    assert result.violations == []
    assert result.performance.error_rate == 0
    assert result.performance.rejection_rate == 0.95
    assert all(trial.status == "completed" and trial.peak_in_flight >= 10 for trial in result.trials[:3])


@pytest.mark.parametrize(
    ("app", "violation", "cause"),
    [
        (SLOPPY_FIX, "invariant.orphaned_resource", "missing_compensation"),
        (REJECT_ALL, "invariant.liveness_failed", "over_rejection"),
        (NO_PAYMENT, "invariant.integrity_violated", "required_step_bypassed"),
    ],
)
def test_false_fixes_fail(project, target, app, violation, cause):
    target.start(app)
    result = run(project, FAST)
    assert result.verdict == "FAIL"
    assert violation_types(result) == {violation}
    assert cause in causes(result)


# Retests


def test_bug_then_fix_is_fixed_and_inherits_the_plan(project, target):
    target.start(BUGGY)
    baseline = run(project, RunOptions(trials=2, serial_control=True))
    target.start(REFERENCE_FIX)
    candidate = run(project, RunOptions(compare_to=baseline.run_id))
    assert candidate.verdict == "PASS"
    assert candidate.config.trials == 2
    assert candidate.config.serial_control is True
    assert candidate.config.config_hash == baseline.config.config_hash
    results = {item.name: item.result for item in candidate.comparison.invariants}
    assert results[CAPACITY] == "FIXED"
    assert results[ACCOUNTING] == "FIXED"
    assert candidate.comparison.fix_verified is True
    assert candidate.target.build.build_id != baseline.target.build.build_id
    stored = report.read_report(project, candidate.run_id)
    assert stored.comparison == candidate.comparison


def test_sloppy_fix_fixes_capacity_but_regresses_orphans(project, target):
    target.start(BUGGY)
    baseline = run(project, FAST)
    target.start(SLOPPY_FIX)
    candidate = run(project, RunOptions(compare_to=baseline.run_id))
    assert candidate.verdict == "FAIL"
    results = {item.name: item.result for item in candidate.comparison.invariants}
    assert results[CAPACITY] == "FIXED"
    assert results["No customer holds a payment authorization without a ticket"] == "REGRESSED"
    assert candidate.comparison.fix_verified is False


def test_rerun_of_unchanged_bug_is_still_failing(project, target):
    target.start(BUGGY)
    baseline = run(project, FAST)
    candidate = run(project, RunOptions(compare_to=baseline.run_id))
    results = {item.name: item.result for item in candidate.comparison.invariants}
    assert results[CAPACITY] == "STILL_FAILING"


def run_directories(project: Path) -> list[Path]:
    return list((project / ".faultforge" / "runs").iterdir())


@pytest.mark.parametrize(
    "options",
    [
        {"vus": 1},
        {"iterations": 21},
        {"max_duration_s": 9},
        {"trials": 2},
        {"serial_control": True},
    ],
)
def test_retest_with_a_different_plan_sends_no_load(project, target, options):
    target.start(BUGGY)
    baseline = run(project, FAST)
    before_runs = run_directories(project)
    target.server.reset(1)
    with pytest.raises(NotComparable) as caught:
        run(project, RunOptions(compare_to=baseline.run_id, **options))
    assert caught.value.comparison.status == "NOT_COMPARABLE"
    assert run_directories(project) == before_runs
    assert target.server.state()["orders_confirmed"] == 0


def test_retest_after_changing_the_workload_sends_no_load(project, target):
    target.start(BUGGY)
    baseline = run(project, FAST)
    def weaken(raw):
        raw["scenarios"][0]["invariants"] = raw["scenarios"][0]["invariants"][-1:]
    write_project(project, f"http://127.0.0.1:{target.port}", weaken)
    target.server.reset(1)
    with pytest.raises(NotComparable) as caught:
        run(project, RunOptions(compare_to=baseline.run_id))
    assert caught.value.comparison.not_comparable_reason == "config_hash differs"
    assert target.server.state()["orders_confirmed"] == 0


def test_config_hash_ignores_the_port(tmp_path, target):
    target.start(REFERENCE_FIX)
    first = write_project(tmp_path / "a", f"http://127.0.0.1:{target.port}")
    second = write_project(tmp_path / "b", "http://127.0.0.1:1")
    from faultforge.config import load_config
    version = "2.3.0"
    hashes = set()
    for path in (first, second):
        loaded = load_config(path)
        scenario = loaded.scenarios[0]
        hashes.add(lifecycle.config_hash(loaded, scenario, scenario.load, version))
    assert len(hashes) == 1


def test_compare_to_unknown_run_is_rejected(project, target):
    target.start(REFERENCE_FIX)
    with pytest.raises(FaultForgeError) as caught:
        run(project, RunOptions(compare_to="run_20200101_000000_abcd"))
    assert caught.value.error_class == "run_not_found"


# Contention


def test_a_serial_rerun_cannot_pass_as_a_concurrent_test(project, target):
    target.start(REFERENCE_FIX)
    result = run(project, RunOptions(trials=1, serial_control=False, vus=1, iterations=5))
    assert result.verdict == "INCONCLUSIVE"
    assert result.trials[0].incomplete_reason == "insufficient_contention"


def test_a_buggy_serial_rerun_cannot_pass_either(project, target):
    target.start(BUGGY)
    result = run(project, RunOptions(trials=1, serial_control=False, vus=1, iterations=5))
    assert result.verdict == "INCONCLUSIVE"


# Evidence


def test_evidence_matches_the_report(project, target):
    target.start(BUGGY)
    result = run(project, RunOptions(trials=2, serial_control=True))
    directory = project / ".faultforge" / "runs" / result.run_id
    content = (directory / "requests.jsonl").read_bytes()
    assert result.evidence.requests.sha256 == hashlib.sha256(content).hexdigest()
    assert result.evidence.requests.count == len(content.splitlines()) == 60
    assert (directory / "report.md").read_text().startswith(f"# {SCENARIO}: FAIL")
    assert (directory / "trials" / "1" / "state-before.json").exists()
    assert (directory / "trials" / "1" / "state-after.json").exists()
    sample = result.violations[0].evidence.sample
    assert 0 < len(sample) <= 10
    assert all(item.outcome == "accepted" for item in sample)
    assert result.violations[0].facts["accepted_overlap"] is True


def test_p95_matches_k6_for_a_single_trial(project, target):
    target.start(REFERENCE_FIX)
    result = run(project, FAST)
    summary = json.loads(
        (project / ".faultforge" / "runs" / result.run_id / "trials" / "1" / "k6-summary.json").read_text()
    )
    k6_p95 = summary["metrics"]["ff_req"]["values"]["p(95)"]
    assert abs(result.performance.latency_ms.p95 - k6_p95) <= 1


def test_disclosures_are_reported(project, target):
    target.start(REFERENCE_FIX)
    result = run(project, FAST)
    joined = " ".join(result.disclosures)
    assert "mutates data" in joined and "stub" in joined and "own hook" in joined


# Failure paths, using a target whose behaviour is scripted


# The first state call after load: preflight, the idle check before reset, the before-snapshot, then this.
AFTER_LOAD_CALL = 4


class Scripted(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self):
        super().__init__(("127.0.0.1", 0), ScriptedHandler)
        self.reserve_mode = "ok"
        self.state_mode = "ok"
        self.reset_mode = "ok"
        self.state_calls = 0
        self.reset_calls = 0
        self.reserve_calls = 0
        self.hits = 0
        self.state_calls_at_reset = []
        self.accept_encoding = None
        self.redirect_to = "http://127.0.0.1:1/"
        self.build_id = "b1"
        self.stock = 1
        self.orders = 0
        self.lock = threading.Lock()
        threading.Thread(target=self.serve_forever, daemon=True).start()

    @property
    def origin(self) -> str:
        return f"http://127.0.0.1:{self.server_address[1]}"

    def handle_error(self, request, client_address):
        """Clients disconnecting from a hanging endpoint is expected."""


class ScriptedHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def reply(self, status, payload=None, headers=()):
        body = json.dumps(payload if payload is not None else {}).encode()
        self.send_response(status)
        self.send_header("Content-Length", str(len(body)))
        for key, value in headers:
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def state_payload(self, call):
        stock = 1
        payload = {
            "stock": stock, "orders_confirmed": 0, "payments_unattached": 0,
            "orders_without_payment": 0, "in_flight": 0,
            "build": {
                "build_id": self.server.build_id, "instance_id": "i1",
                "instance_started_at": "t", "stale_source": False,
            },
        }
        mode = self.server.state_mode
        load_started = self.server.reserve_calls > 0
        if mode == "live":
            payload["stock"] = self.server.stock
            payload["orders_confirmed"] = self.server.orders
        if mode == "draining" and call <= 3:
            payload["in_flight"] = 3
        if mode == "busy_after_reset" and self.server.reset_calls > 0:
            payload["in_flight"] = 3
        if mode == "busy_after_load" and load_started:
            payload["in_flight"] = 3
        if mode == "no_instance" or (mode == "instance_lost" and load_started):
            del payload["build"]["instance_id"]
        if mode == "instance_appears" and not load_started:
            del payload["build"]["instance_id"]
        if mode == "no_build":
            del payload["build"]
        if mode == "surrogate_build":
            payload["build"]["build_id"] = "\ud800"
        if mode == "surrogate_key":
            payload["\ud800"] = 5
        if mode == "restarting":
            payload["build"]["instance_id"] = f"i{call}"
        if mode == "busy":
            payload["in_flight"] = 3
        if mode == "stale_reset":
            payload["stock"] = 99
        if mode == "missing_field":
            del payload["orders_without_payment"]
        return payload

    def drip(self, head):
        """Sends one byte at a time, so no single read ever times out."""
        self.close_connection = True
        try:
            self.wfile.write(head)
            for _ in range(300):
                self.wfile.write(b"a")
                time.sleep(0.1)
        except OSError:
            pass

    def do_GET(self):
        server = self.server
        with server.lock:
            server.hits += 1
        server.accept_encoding = self.headers.get("accept-encoding")
        if self.path == "/health":
            self.reply(200)
        elif self.path == "/__ff/state":
            with server.lock:
                server.state_calls += 1
                call = server.state_calls
            mode = server.state_mode
            if mode == "redirect":
                self.reply(302, headers=[("Location", server.redirect_to)])
            elif mode == "fails_after_preflight" and call > AFTER_LOAD_CALL - 1:
                self.reply(500)
            elif mode == "hangs_after_preflight" and call > AFTER_LOAD_CALL - 1:
                time.sleep(10)
            elif mode == "drip_body" or (mode == "drip_body_after_load" and server.reserve_calls > 0):
                self.drip(b"HTTP/1.1 200 OK\r\nContent-Length: 200000\r\n\r\n")
            elif mode == "drip_headers" or (mode == "drip_headers_after_load" and server.reserve_calls > 0):
                self.drip(b"HTTP/1.1 200 OK\r\nX-Drip: ")
            elif mode == "gzip":
                body = gzip.compress(json.dumps(self.state_payload(call)).encode())
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Content-Encoding", "gzip")
                self.end_headers()
                self.wfile.write(body)
            elif mode == "slow":
                time.sleep(0.6)
                self.reply(200, self.state_payload(call))
            elif mode == "not_json":
                body = b"nope"
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            else:
                self.reply(200, self.state_payload(call))
        else:
            self.reply(404)

    def do_POST(self):
        self.rfile.read(int(self.headers.get("content-length", 0)))
        server = self.server
        with server.lock:
            server.hits += 1
        if self.path == "/__ff/reset":
            with server.lock:
                server.reset_calls += 1
                server.state_calls_at_reset.append(server.state_calls)
                server.stock, server.orders = 1, 0
            self.reply(500 if server.reset_mode == "fail" else 200)
        elif self.path == "/reserve":
            with server.lock:
                server.reserve_calls += 1
            if server.reserve_mode == "hang":
                time.sleep(60)
            time.sleep(0.05)
            if server.reserve_mode == "all_win":
                with server.lock:
                    server.orders += 1
                self.reply(200, {"order_id": "o"})
            elif server.reserve_mode in ("atomic_500", "atomic_409"):
                with server.lock:
                    won = server.stock > 0
                    if won:
                        server.stock -= 1
                        server.orders += 1
                loser = 500 if server.reserve_mode == "atomic_500" else 409
                self.reply(200 if won else loser, {"order_id": "o"})
            else:
                self.reply(500 if server.reserve_mode == "boom" else 409)


@pytest.fixture
def scripted():
    server = Scripted()
    yield server
    server.shutdown()
    server.server_close()


@pytest.fixture
def scripted_project(tmp_path, scripted):
    return write_project(tmp_path / "scripted", scripted.origin)


def test_target_down_is_a_structured_error(tmp_path):
    project = write_project(tmp_path / "project", f"http://127.0.0.1:{free_port()}")
    with pytest.raises(FaultForgeError) as caught:
        run(project)
    assert caught.value.error_class == "target_unreachable"
    assert "Start the target" in caught.value.fix_hint


def test_redirecting_hook_is_refused_before_any_load(scripted, scripted_project):
    scripted.state_mode = "redirect"
    with pytest.raises(FaultForgeError) as caught:
        run(scripted_project)
    assert caught.value.error_class == "target_unreachable"
    assert scripted.reset_calls == 0 and scripted.reserve_calls == 0


def test_invariant_using_an_unreported_field_is_refused_before_any_load(scripted, scripted_project):
    scripted.state_mode = "missing_field"
    with pytest.raises(FaultForgeError) as caught:
        run(scripted_project)
    assert caught.value.error_class == "config_invalid"
    assert scripted.reset_calls == 0 and scripted.reserve_calls == 0


def test_over_budget_plan_sends_nothing(scripted, scripted_project):
    with pytest.raises(FaultForgeError) as caught:
        run(scripted_project, RunOptions(trials=5, iterations=500, serial_control=False))
    assert caught.value.error_class == "budget_exceeded"
    assert caught.value.suggested_plan
    assert scripted.state_calls == 0 and scripted.reset_calls == 0 and scripted.reserve_calls == 0


def test_second_run_is_busy(project, target):
    target.start(REFERENCE_FIX)
    with report.run_lock(project), pytest.raises(FaultForgeError) as caught:
        run(project, FAST)
    assert caught.value.error_class == "busy"


def test_failed_reset_is_incomplete_and_sends_no_load(scripted, scripted_project):
    scripted.reset_mode = "fail"
    result = run(scripted_project, FAST)
    assert result.verdict == "INCONCLUSIVE"
    assert result.trials[0].incomplete_reason == "reset_failed"
    assert scripted.reserve_calls == 0


def test_reset_that_leaves_stale_data_is_incomplete(scripted, scripted_project):
    scripted.state_mode = "stale_reset"
    result = run(scripted_project, FAST)
    assert result.trials[0].incomplete_reason == "reset_failed"
    assert scripted.reserve_calls == 0


def test_restart_during_a_trial_is_detected(scripted, scripted_project):
    scripted.state_mode = "restarting"
    result = run(scripted_project, FAST)
    assert result.trials[0].incomplete_reason == "target_restarted"
    assert result.verdict != "PASS"


def test_target_that_never_settles_is_incomplete(scripted, scripted_project):
    scripted.state_mode = "busy_after_load"
    result = run(scripted_project, FAST)
    assert result.trials[0].incomplete_reason == "state_snapshot_failed"
    assert result.verdict != "PASS"


def test_state_hook_failing_after_load_is_incomplete(scripted, scripted_project):
    scripted.state_mode = "fails_after_preflight"
    result = run(scripted_project, FAST)
    assert result.trials[0].incomplete_reason == "state_snapshot_failed"
    assert result.verdict != "PASS"


def test_hanging_target_is_inconclusive_within_budget(scripted, scripted_project):
    scripted.reserve_mode = "hang"
    started = time.monotonic()
    result = run(scripted_project, RunOptions(trials=1, serial_control=False, max_duration_s=1))
    assert time.monotonic() - started < 15
    assert result.verdict == "INCONCLUSIVE"
    assert result.trials[0].incomplete_reason == "iterations_short"


def test_server_errors_are_a_proven_failure(scripted, scripted_project):
    scripted.reserve_mode = "boom"
    result = run(scripted_project, FAST)
    assert result.verdict == "FAIL"
    assert "response.unexpected_status" in violation_types(result)


def test_budget_exhaustion_skips_later_trials(scripted, scripted_project, monkeypatch):
    def slow_trial(spec, trial_dir, requests_path, k6=None, kill_after_s=None):
        time.sleep(5)
        return EngineResult(
            pid=0, exit_code=0, killed=False, iterations_completed=0, iterations_dropped=0,
            records=(), peak_in_flight=0, incomplete_reason="iterations_short",
        )

    monkeypatch.setattr(k6_runner, "run_trial", slow_trial)
    limits = Limits(max_wall_s_total=8, admission_margin_s=0, trial_overhead_s=3)
    started = time.monotonic()
    result = run(
        scripted_project,
        RunOptions(trials=2, serial_control=False, max_duration_s=1),
        limits=limits,
    )
    assert time.monotonic() - started < limits.max_wall_s_total + 5
    assert result.trials[0].status == "incomplete"
    assert (result.trials[1].status, result.trials[1].incomplete_reason) == ("skipped", "budget_exhausted")
    assert result.verdict == "INCONCLUSIVE"
    assert result.completeness.trials_skipped == 1


def test_stuck_target_and_hooks_stay_within_the_wall_budget(scripted, scripted_project):
    scripted.reserve_mode = "hang"
    scripted.state_mode = "hangs_after_preflight"
    limits = Limits(max_wall_s_total=14, admission_margin_s=0, trial_overhead_s=3)
    started = time.monotonic()
    result = run(
        scripted_project,
        RunOptions(trials=2, serial_control=False, max_duration_s=4),
        limits=limits,
    )
    assert time.monotonic() - started < limits.max_wall_s_total + 5
    assert result.verdict == "INCONCLUSIVE"
    assert all(trial.status != "completed" for trial in result.trials)


def test_invalid_engine_results_are_a_structured_error(scripted, scripted_project, monkeypatch):
    def broken(*args, **kwargs):
        from faultforge.models import RequestRecord
        RequestRecord(trial=0, request_id="", vu=0, iter=0, t_start=0, t_end=0, latency_ms=0, status=0, outcome="x")

    monkeypatch.setattr(k6_runner, "run_trial", broken)
    with pytest.raises(FaultForgeError) as caught:
        run(scripted_project, FAST)
    assert caught.value.error_class == "engine_failed"


def test_broken_k6_binary_is_a_structured_error(scripted, scripted_project, tmp_path):
    fake = tmp_path / "bad-k6"
    fake.write_text("#!/bin/sh\nexit 1\n")
    fake.chmod(0o755)
    with pytest.raises(FaultForgeError) as caught:
        run(scripted_project, FAST, k6=fake)
    assert caught.value.error_class == "k6_missing"
    assert scripted.reset_calls == 0


def test_clamped_load_is_reported(project, target):
    target.start(REJECT_ALL)
    result = run(project, RunOptions(trials=1, serial_control=False, vus=500, iterations=100))
    assert result.config.load.vus == LIMITS.max_vus
    assert [(c.field, c.requested, c.effective) for c in result.budget.clamped] == [("vus", 500, 50)]
    assert result.budget.requests_sent == 100


def test_describe_scenarios_sends_no_load(scripted, scripted_project):
    described = lifecycle.describe_scenarios(scripted_project, [scripted_project])
    assert described["target"]["reachable"] is True
    assert described["scenarios"][0]["mutates_data"] is True
    assert described["limits"]["max_vus"] == 50
    assert scripted.reset_calls == 0 and scripted.reserve_calls == 0


# CLI


def test_cli_exit_codes(project, target, capsys):
    target.start(BUGGY)
    assert cli_main(["--project", str(project), "run", SCENARIO, "--trials", "1", "--no-serial-control"]) == 1
    output = capsys.readouterr().out
    assert "FAIL (1/1 trials complete)" in output
    run_id = output.rsplit("run_id: ", 1)[1].strip()

    target.start(REFERENCE_FIX)
    assert cli_main(["--project", str(project), "run", SCENARIO, "--compare-to", run_id]) == 0
    assert "FIXED" in capsys.readouterr().out

    assert cli_main(["--project", str(project), "show", run_id]) == 1
    assert "## Proven" in capsys.readouterr().out
    assert cli_main(["--project", str(project), "list"]) == 0
    assert json.loads(capsys.readouterr().out)["scenarios"][0]["id"] == SCENARIO


def test_cli_returns_errors_as_data(tmp_path, capsys):
    project = write_project(tmp_path / "project", f"http://127.0.0.1:{free_port()}")
    assert cli_main(["--project", str(project), "run", SCENARIO]) == 3
    assert json.loads(capsys.readouterr().out)["error_class"] == "target_unreachable"


def test_cli_retest_with_a_different_plan_is_not_comparable(project, target, capsys):
    target.start(BUGGY)
    assert cli_main(["--project", str(project), "run", SCENARIO, "--trials", "1", "--no-serial-control"]) == 1
    run_id = capsys.readouterr().out.rsplit("run_id: ", 1)[1].strip()
    assert cli_main(["--project", str(project), "run", SCENARIO, "--compare-to", run_id, "--vus", "1"]) == 4
    assert json.loads(capsys.readouterr().out)["status"] == "NOT_COMPARABLE"


@pytest.mark.parametrize("sent", [signal.SIGTERM, signal.SIGHUP])
def test_signals_stop_k6(scripted, scripted_project, sent):
    scripted.reserve_mode = "hang"
    process = subprocess.Popen(
        [sys.executable, "-m", "faultforge.cli", "--project", str(scripted_project),
         "run", SCENARIO, "--trials", "1", "--no-serial-control"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.monotonic() + 10
        while scripted.reserve_calls == 0 and time.monotonic() < deadline:
            time.sleep(0.05)
        assert scripted.reserve_calls > 0
        children = subprocess.run(
            ["pgrep", "-f", str(k6_runner.SCRIPTS["burst_request"])], capture_output=True, text=True
        ).stdout.split()
        assert children
        process.send_signal(sent)
        assert process.wait(timeout=10) == 128 + sent
        for pid in children:
            with pytest.raises(ProcessLookupError):
                for _ in range(100):
                    os.kill(int(pid), 0)
                    time.sleep(0.05)
    finally:
        process.kill()
        process.wait()


# Hostile hook responses


def hooks_for(server, timeout_s=0.5) -> TargetHooks:
    return TargetHooks(TargetConfig(origin=server.origin, health="/health", state="/__ff/state"), timeout_s)


@pytest.mark.parametrize("mode", ["drip_body", "drip_headers"])
def test_dripping_hook_stops_at_its_total_deadline(scripted, mode):
    scripted.state_mode = mode
    started = time.monotonic()
    with pytest.raises(HookError, match="deadline"):
        hooks_for(scripted).state()
    assert time.monotonic() - started < 1


@pytest.mark.parametrize("mode", ["drip_body_after_load", "drip_headers_after_load"])
def test_dripping_hook_cannot_outlast_the_wall_budget(scripted, scripted_project, mode):
    scripted.state_mode = mode
    limits = Limits(max_wall_s_total=10, admission_margin_s=0, hook_timeout_s=1)
    started = time.monotonic()
    result = run(scripted_project, RunOptions(trials=1, serial_control=False, max_duration_s=3), limits=limits)
    assert time.monotonic() - started < limits.max_wall_s_total
    assert result.trials[0].incomplete_reason == "state_snapshot_failed"
    assert scripted.reserve_calls > 0


def test_time_spent_in_hooks_cannot_push_load_past_the_wall_budget(scripted, scripted_project):
    scripted.state_mode = "slow"
    limits = Limits(max_wall_s_total=7.5, admission_margin_s=0)
    result = run(scripted_project, RunOptions(trials=1, serial_control=False, max_duration_s=1), limits=limits)
    assert result.trials[0].status == "skipped"
    assert result.verdict == "ERROR"
    assert scripted.reserve_calls == 0


def test_encoded_hook_response_is_refused_without_decoding(scripted):
    scripted.state_mode = "gzip"
    with pytest.raises(HookError, match="encoded"):
        hooks_for(scripted).state()
    assert scripted.accept_encoding == "identity"


@pytest.mark.parametrize("mode", ["surrogate_build", "surrogate_key"])
def test_lone_surrogates_from_the_target_cannot_break_the_report(scripted, scripted_project, mode):
    scripted.state_mode = mode
    result = run(scripted_project, FAST)
    assert result.target.build.build_id != "\ud800"
    assert "\ud800" not in result.trials[0].state_before
    assert report.read_report(scripted_project, result.run_id).run_id == result.run_id


def test_redirect_to_a_live_loopback_server_is_not_followed(scripted, scripted_project):
    other = Scripted()
    try:
        scripted.state_mode = "redirect"
        scripted.redirect_to = f"{other.origin}/__ff/state"
        with pytest.raises(FaultForgeError) as caught:
            run(scripted_project)
        assert caught.value.error_class == "target_unreachable"
        assert other.hits == 0
        assert scripted.reset_calls == 0 and scripted.reserve_calls == 0
    finally:
        other.shutdown()
        other.server_close()


# Crashes stay structured


def test_bad_usage_does_not_exit_with_the_inconclusive_code(capsys):
    assert cli_main(["run"]) == 3
    assert cli_main(["no-such-command"]) == 3


def test_unexpected_crash_is_an_engine_failure_not_a_fail(tmp_path, monkeypatch, capsys):
    def crash(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(lifecycle, "run_scenario", crash)
    project = write_project(tmp_path / "project", f"http://127.0.0.1:{free_port()}")
    assert cli_main(["--project", str(project), "run", SCENARIO]) == 3
    assert json.loads(capsys.readouterr().out)["error_class"] == "engine_failed"


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores directory permissions")
def test_read_only_project_is_an_engine_failure(scripted, scripted_project):
    scripted_project.chmod(0o555)
    try:
        with pytest.raises(FaultForgeError) as caught:
            run(scripted_project, FAST)
    finally:
        scripted_project.chmod(0o755)
    assert caught.value.error_class == "engine_failed"
    assert scripted.reserve_calls == 0


def test_os_error_during_a_trial_is_an_engine_failure(scripted, scripted_project, monkeypatch):
    def out_of_space(*args, **kwargs):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(k6_runner, "run_trial", out_of_space)
    with pytest.raises(FaultForgeError) as caught:
        run(scripted_project, FAST)
    assert caught.value.error_class == "engine_failed"


def test_corrupt_report_is_run_not_found(scripted, scripted_project):
    result = run(scripted_project, FAST)
    path = scripted_project / ".faultforge" / "runs" / result.run_id / "report.json"
    path.write_bytes(b"\xff\xfe not utf-8")
    with pytest.raises(FaultForgeError) as caught:
        report.read_report(scripted_project, result.run_id)
    assert caught.value.error_class == "run_not_found"


def test_written_artifacts_are_readable_by_another_uid(scripted, scripted_project):
    result = run(scripted_project, FAST)
    directory = scripted_project / ".faultforge" / "runs" / result.run_id
    for name in ("report.json", "report.md", "trials/1/state-before.json", "trials/1/state-after.json"):
        assert stat.S_IMODE((directory / name).stat().st_mode) == 0o644


# Verdict correctness


def test_fixed_invariants_with_unexpected_500s_are_not_a_verified_fix(scripted, scripted_project, capsys):
    project = str(scripted_project)
    scripted.state_mode = "live"
    scripted.reserve_mode = "all_win"
    baseline = run(scripted_project, FAST)
    assert baseline.verdict == "FAIL"

    scripted.build_id = "b2"
    scripted.reserve_mode = "atomic_500"
    with_500s = run(scripted_project, RunOptions(compare_to=baseline.run_id))
    assert with_500s.verdict == "FAIL"
    results = {item.name: item.result for item in with_500s.comparison.invariants}
    assert results[CAPACITY] == "FIXED"
    assert with_500s.comparison.fix_verified is False
    summary = scripted_project / ".faultforge" / "runs" / with_500s.run_id / "report.md"
    assert "Fix verified: no" in summary.read_text()
    assert cli_main(["--project", project, "compare", baseline.run_id, with_500s.run_id]) == 1

    scripted.build_id = "b3"
    scripted.reserve_mode = "atomic_409"
    clean = run(scripted_project, RunOptions(compare_to=baseline.run_id))
    assert clean.verdict == "PASS"
    assert clean.comparison.fix_verified is True
    summary = scripted_project / ".faultforge" / "runs" / clean.run_id / "report.md"
    assert "Fix verified: yes" in summary.read_text()
    assert cli_main(["--project", project, "compare", baseline.run_id, clean.run_id]) == 0

    stored = scripted_project / ".faultforge" / "runs" / clean.run_id / "report.json"
    data = json.loads(stored.read_text())
    data["config"]["config_hash"] = "sha256:" + "0" * 64
    stored.write_text(json.dumps(data))
    capsys.readouterr()
    assert cli_main(["--project", project, "compare", baseline.run_id, clean.run_id]) == 4
    assert json.loads(capsys.readouterr().out)["fix_verified"] is False


def test_reset_waits_for_stragglers_to_finish(scripted, scripted_project):
    scripted.state_mode = "draining"
    result = run(scripted_project, FAST)
    # Preflight, then two busy polls, then the idle one.
    assert scripted.state_calls_at_reset[0] == 4
    assert result.trials[0].incomplete_reason is None


def test_work_still_in_flight_after_reset_stops_the_trial(scripted, scripted_project):
    scripted.state_mode = "busy_after_reset"
    result = run(scripted_project, FAST)
    assert result.trials[0].incomplete_reason == "reset_failed"
    assert scripted.reserve_calls == 0


def test_target_that_is_never_idle_stops_after_the_settle_cap(scripted, scripted_project):
    scripted.state_mode = "busy"
    limits = Limits(settle_cap_s=0.2)
    result = run(scripted_project, FAST, limits=limits)
    assert result.trials[0].incomplete_reason == "reset_failed"
    assert scripted.reserve_calls == 0
    assert scripted.state_calls > 3


@pytest.mark.parametrize("mode", ["instance_lost", "instance_appears"])
def test_instance_id_on_one_side_only_is_a_restart(scripted, scripted_project, mode):
    scripted.state_mode = mode
    result = run(scripted_project, FAST)
    assert result.trials[0].incomplete_reason == "target_restarted"
    assert RESTART_NOTE not in result.disclosures


@pytest.mark.parametrize("mode", ["no_instance", "no_build"])
def test_no_instance_id_on_either_side_is_disclosed(scripted, scripted_project, mode):
    scripted.state_mode = mode
    result = run(scripted_project, FAST)
    assert result.trials[0].status == "completed"
    assert RESTART_NOTE in result.disclosures


def test_a_reported_instance_id_needs_no_disclosure(scripted, scripted_project):
    result = run(scripted_project, FAST)
    assert RESTART_NOTE not in result.disclosures
