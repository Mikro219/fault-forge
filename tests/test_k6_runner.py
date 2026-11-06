"""M2 acceptance tests. These run the real pinned k6 against small local servers."""

import collections
import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from test_ticket_api import BUGGY, REFERENCE_FIX, Server

from faultforge import k6_runner
from faultforge.evidence import count_outcomes, peak_in_flight
from faultforge.k6_runner import (
    K6Missing,
    TrialSpec,
    UnknownScript,
    build_argv,
    build_env,
    find_k6,
    run_trial,
    script_path,
)
from faultforge.models import LoadConfig, OutcomeConfig, RequestRecord, RequestTemplate

pytestmark = pytest.mark.skipif(
    not (k6_runner.REPO_ROOT / ".tools" / "bin" / "k6").exists(),
    reason="k6 not installed; run scripts/install_k6.sh",
)


class Fixture(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self):
        super().__init__(("127.0.0.1", 0), Handler)
        self.hits = collections.Counter()
        self.bodies = []
        self.headers_seen = []
        self.lock = threading.Lock()
        self.redirect_to = ""
        threading.Thread(target=self.serve_forever, daemon=True).start()

    @property
    def origin(self) -> str:
        return f"http://127.0.0.1:{self.server_address[1]}"

    def handle_error(self, request, client_address):
        """k6 disconnecting from a slow endpoint is expected in these tests."""


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("content-length", 0)))
        server = self.server
        with server.lock:
            server.hits[self.path] += 1
            server.bodies.append(body)
            server.headers_seen.append(self.headers.get("x-ff-request-id"))
            count = server.hits[self.path]
        if self.path == "/one-winner":
            time.sleep(0.05)
            self.reply(200 if count == 1 else 409, b'{"order_id": 7}')
        elif self.path == "/redirect":
            self.send_response(302)
            self.send_header("Location", f"{server.redirect_to}/landed")
            self.send_header("Content-Length", "0")
            self.end_headers()
        elif self.path == "/boom":
            self.reply(500, b"{}")
        elif self.path == "/slow":
            time.sleep(1.5)
            self.reply(200, b"{}")
        elif self.path == "/hang":
            time.sleep(60)
        elif self.path == "/delay":
            time.sleep(0.05)
            self.reply(200, b'{"order_id": 1}')
        else:
            self.reply(200, b'{"order_id": 1}')

    def reply(self, status, body):
        self.send_response(status)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture
def server():
    fixture = Fixture()
    yield fixture
    fixture.shutdown()
    fixture.server_close()


def make_spec(origin, path="/", vus=20, iterations=20, max_s=10, kind="concurrent", min_peak=None,
              capture="order_id", body=None):
    return TrialSpec(
        origin=origin,
        request=RequestTemplate(method="POST", path=path, json=body or {"buyer": "buyer-${VU}-${ITER}"}),
        outcomes=OutcomeConfig(accepted_status=[200, 201], rejected_status=[409], capture_field=capture),
        load=LoadConfig(vus=vus, iterations=iterations, max_duration_s=max_s, min_peak_in_flight=min_peak),
        trial=1,
        kind=kind,
    )


def run(spec, tmp_path, **kwargs):
    return run_trial(spec, tmp_path / "trial", tmp_path / "requests.jsonl", **kwargs)


def error_rate(result) -> float:
    counts = count_outcomes(result.records)
    return (counts.unexpected + counts.transport_error) / len(result.records)


# 409s are expected rejections, not errors


def test_rejections_are_not_errors(server, tmp_path):
    result = run(make_spec(server.origin, "/one-winner"), tmp_path)
    counts = count_outcomes(result.records)
    assert (counts.accepted, counts.rejected, counts.unexpected, counts.transport_error) == (1, 19, 0, 0)
    assert error_rate(result) == 0
    summary = json.loads((tmp_path / "trial" / "k6-summary.json").read_text())
    assert summary["metrics"]["http_req_failed"]["values"]["rate"] == 0
    assert result.incomplete_reason is None


def test_server_errors_are_unexpected_not_transport_errors(server, tmp_path):
    result = run(make_spec(server.origin, "/boom", vus=1, iterations=2, kind="serial_control"), tmp_path)
    counts = count_outcomes(result.records)
    assert (counts.unexpected, counts.transport_error) == (2, 0)
    assert {record.status for record in result.records} == {500}


# Requests can't leave loopback


def test_redirects_are_not_followed(server, tmp_path):
    listener = Fixture()
    try:
        server.redirect_to = listener.origin
        result = run(make_spec(server.origin, "/redirect", vus=1, iterations=1, kind="serial_control"), tmp_path)
        assert count_outcomes(result.records).unexpected == 1
        assert result.records[0].status == 302
        assert listener.hits["/landed"] == 0
    finally:
        listener.shutdown()
        listener.server_close()


@pytest.mark.parametrize(
    "origin", ["http://10.255.255.1:9", "http://1.1.1.1:80", "http://[::1]:9"]
)
def test_non_loopback_addresses_are_blocked(origin, tmp_path):
    result = run(make_spec(origin, "/x", vus=1, iterations=1, max_s=5, kind="serial_control"), tmp_path)
    assert count_outcomes(result.records).transport_error == 1
    assert "blacklisted range" in (tmp_path / "trial" / "k6.log").read_text()


def test_loopback_is_not_blocked(server, tmp_path):
    result = run(make_spec(server.origin, "/ok", vus=1, iterations=1, kind="serial_control"), tmp_path)
    assert count_outcomes(result.records).accepted == 1


# The environment is sanitised


def test_ambient_k6_settings_never_reach_k6(server, tmp_path, monkeypatch):
    monkeypatch.setenv("K6_OUT", "cloud")
    monkeypatch.setenv("K6_CLOUD_TOKEN", "secret-token-value")
    monkeypatch.setenv("VULTR_API_KEY", "secret-key-value")
    spec = make_spec(server.origin, "/ok", vus=1, iterations=1, kind="serial_control")

    env = build_env(tmp_path)
    argv = build_argv(find_k6(), spec, tmp_path / "raw.json", tmp_path / "summary.json")
    assert set(env) == {"PATH", "HOME", "TMPDIR"}
    joined = " ".join(argv)
    assert "cloud" not in joined and "secret" not in joined
    assert "--no-usage-report" in argv

    result = run(spec, tmp_path)
    assert result.exit_code == 0
    assert "cloud" not in (tmp_path / "trial" / "k6.log").read_text().lower()


# Hangs and short runs


def assert_process_group_gone(pid: int) -> None:
    with pytest.raises(ProcessLookupError):
        os.killpg(pid, 0)


def test_deadline_kills_k6_and_leaves_no_process(server, tmp_path):
    started = time.monotonic()
    spec = make_spec(server.origin, "/hang", vus=2, iterations=2, max_s=20)
    result = run(spec, tmp_path, kill_after_s=1)
    assert time.monotonic() - started < 6
    assert result.killed is True
    assert result.incomplete_reason == "timeout"
    assert_process_group_gone(result.pid)


def test_slow_target_is_short_even_though_k6_exits_zero(server, tmp_path):
    result = run(make_spec(server.origin, "/slow", vus=1, iterations=10, max_s=2, kind="serial_control"), tmp_path)
    assert result.exit_code == 0
    assert result.iterations_dropped > 0
    assert result.incomplete_reason == "iterations_short"


def test_hanging_target_with_no_finished_iterations_is_short(server, tmp_path):
    result = run(make_spec(server.origin, "/hang", vus=2, iterations=2, max_s=1), tmp_path)
    assert result.exit_code == 0
    assert result.records == ()
    assert result.incomplete_reason == "iterations_short"


def test_nonzero_exit_is_reported(tmp_path, server):
    fake = tmp_path / "fake-k6"
    fake.write_text("#!/bin/sh\nexit 107\n")
    fake.chmod(0o755)
    result = run(make_spec(server.origin), tmp_path, k6=fake)
    assert result.exit_code == 107
    assert result.incomplete_reason == "engine_exit_nonzero"


# Contention


def test_low_concurrency_is_insufficient_contention(server, tmp_path):
    result = run(make_spec(server.origin, "/delay", vus=1, iterations=3, min_peak=2), tmp_path)
    assert result.peak_in_flight == 1
    assert result.incomplete_reason == "insufficient_contention"


def test_serial_control_is_exempt_from_contention(server, tmp_path):
    result = run(make_spec(server.origin, "/delay", vus=1, iterations=3, min_peak=2, kind="serial_control"), tmp_path)
    assert result.incomplete_reason is None


def test_peak_in_flight_ignores_touching_intervals():
    def record(start, end):
        return RequestRecord(
            trial=1, request_id="r", vu=1, iter=0, t_start=start, t_end=end,
            latency_ms=end - start, status=200, outcome="accepted",
        )

    assert peak_in_flight([record(0, 10), record(10, 20)]) == 1
    assert peak_in_flight([record(0, 10), record(5, 20), record(6, 7)]) == 3
    assert peak_in_flight([]) == 0


# Evidence


def test_evidence_is_complete_and_tidy(server, tmp_path):
    result = run(make_spec(server.origin, "/one-winner"), tmp_path)
    lines = (tmp_path / "requests.jsonl").read_text().splitlines()
    assert len(lines) == 20
    records = [RequestRecord.model_validate_json(line) for line in lines]
    assert records == list(result.records)
    assert all(record.t_end >= record.t_start > 0 for record in records)
    assert len({record.request_id for record in records}) == 20
    assert [record.captured for record in records if record.outcome == "accepted"] == ["7"]
    assert all(record.captured is None for record in records if record.outcome != "accepted")
    assert sorted(path.name for path in (tmp_path / "trial").iterdir()) == ["k6-summary.json", "k6.log"]


def test_trials_append_to_one_evidence_file(server, tmp_path):
    spec = make_spec(server.origin, "/ok", vus=1, iterations=2, kind="serial_control")
    run_trial(spec, tmp_path / "t1", tmp_path / "requests.jsonl")
    run_trial(spec, tmp_path / "t2", tmp_path / "requests.jsonl")
    assert len((tmp_path / "requests.jsonl").read_text().splitlines()) == 4


def test_summary_has_no_per_request_values(server, tmp_path):
    run(make_spec(server.origin, "/one-winner"), tmp_path)
    summary = (tmp_path / "trial" / "k6-summary.json").read_text()
    assert "t1-0" not in summary
    assert "ff_request_id" not in summary


def test_oversized_log_keeps_only_its_tail(tmp_path):
    log = tmp_path / "k6.log"
    log.write_bytes(b"a" * 10 + b"b" * k6_runner.LOG_TAIL_BYTES)
    k6_runner._cap_log(log)
    assert log.read_bytes() == b"b" * k6_runner.LOG_TAIL_BYTES


def test_truncated_final_line_is_skipped(tmp_path):
    raw = tmp_path / "raw.json"
    raw.write_text('{"type":"Point","metric":"http_reqs","data":{}}\n{"type":"Point","metr')
    assert k6_runner.read_records(raw) == []


# Templating


def test_templates_substitute_in_json_values_only(server, tmp_path):
    body = {"buyer": "b-${VU}-${ITER}", "nested": {"tags": ["x-${VU}"]}, "count": 3}
    run(make_spec(server.origin, "/ok", vus=2, iterations=2, kind="serial_control", body=body), tmp_path)
    decoded = [json.loads(raw) for raw in server.bodies]
    assert {item["buyer"] for item in decoded} == {"b-1-0", "b-2-0"}
    assert {item["nested"]["tags"][0] for item in decoded} == {"x-1", "x-2"}
    assert {item["count"] for item in decoded} == {3}
    assert len(set(server.headers_seen)) == 2


def test_no_capture_field_discards_bodies(server, tmp_path):
    spec = make_spec(server.origin, "/ok", vus=1, iterations=1, kind="serial_control", capture=None)
    result = run(spec, tmp_path)
    assert result.records[0].captured is None
    assert result.records[0].outcome == "accepted"


# Lookup errors


def test_unknown_script_is_rejected():
    with pytest.raises(UnknownScript):
        script_path("../../etc/passwd")


def test_k6_version_matches_the_pin():
    lock = (k6_runner.REPO_ROOT / "k6.lock").read_text(encoding="utf-8")
    assert f"version={k6_runner.k6_version(find_k6())}\n" in lock


def test_missing_k6_is_reported(monkeypatch, tmp_path):
    monkeypatch.delenv("FF_K6_PATH", raising=False)
    monkeypatch.setattr(k6_runner, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(k6_runner.shutil, "which", lambda name: None)
    with pytest.raises(K6Missing):
        find_k6()


# The real target, driven by k6


TICKET_SPEC = dict(
    request=RequestTemplate(method="POST", path="/reserve", json={"item_id": 1, "buyer": "buyer-${VU}-${ITER}"}),
    outcomes=OutcomeConfig(accepted_status=[200, 201], rejected_status=[409], capture_field="order_id"),
    load=LoadConfig(vus=20, iterations=20, max_duration_s=10, min_peak_in_flight=10),
    trial=1,
)


def run_against(app_dir, tmp_path, attempts):
    target = Server(app_dir, tmp_path / "ticket.db").start()
    try:
        results = []
        for attempt in range(attempts):
            target.reset(1)
            directory = tmp_path / f"attempt-{attempt}"
            result = run_trial(
                TrialSpec(origin=target.url, **TICKET_SPEC), directory / "trial", tmp_path / f"{attempt}.jsonl"
            )
            results.append((result, target.state()))
        return results
    finally:
        target.stop()


def test_k6_reproduces_the_oversell_on_buggy_code(tmp_path):
    for result, state in run_against(BUGGY, tmp_path, attempts=10):
        counts = count_outcomes(result.records)
        assert result.incomplete_reason is None
        assert counts.accepted > 1
        assert counts.accepted == state["orders_confirmed"]
        assert state["stock"] == 0
        assert (counts.unexpected, counts.transport_error) == (0, 0)
        assert result.peak_in_flight >= 10


def test_k6_confirms_one_sale_on_the_reference_fix(tmp_path):
    for result, state in run_against(REFERENCE_FIX, tmp_path, attempts=10):
        counts = count_outcomes(result.records)
        assert result.incomplete_reason is None
        assert (counts.accepted, counts.rejected, counts.unexpected, counts.transport_error) == (1, 19, 0, 0)
        assert state["orders_confirmed"] == 1
        assert state["payments_unattached"] == 0
        assert result.peak_in_flight >= 10
