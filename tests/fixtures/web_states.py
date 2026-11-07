"""API responses for checking the page in a browser, one per state. These are test data and never
ship. tests/test_web.py validates every one against the real view models, so the shapes stay real."""

import copy

HOSTILE = '<img src=x onerror="window.__xss=1">'
RUN_ID = "r_" + "a" * 16
OLD_HASH = "b" * 64
NEW_HASH = "c" * 64
FINGERPRINT = "sha256:" + "d" * 64
INVARIANTS = [
    "No more tickets confirmed than were in stock",
    "Stock accounting balances",
    "Every confirmation is recorded",
    "No customer holds a payment authorization without a ticket",
]


def status(**overrides) -> dict:
    budget = {
        "enabled": True, "exhausted": False, "daily_usd_spent": 0.0123, "daily_usd_cap": 2.0, "total_usd_spent": 0.5,
        "total_usd_cap": 15.0, "daily_tokens_spent": 1234, "daily_token_cap": 1000000,
    }
    base = {
        "busy": False, "queue_length": 0, "queue_limit": 3, "cooldown_s": 30.0,
        "targets": [
            {"id": "ticket-api", "title": "Ticket API", "description": "20 buyers race for the last ticket.", "fixable": True},
            {"id": "hostile-build", "title": "Hostile build", "description": "Real code that tries to break out of its sandbox.", "fixable": False},
        ],
        "agent_enabled": True, "agent_disabled_reason": None, "budget": budget,
        "vm": {"healthy": True, "docker_reachable": True, "load_1m": 0.2, "memory_available_mb": 1000, "disk_free_mb": 9000, "containers_left": 0},
        "replays_available": 2,
    }
    return {**base, **overrides}


def container(**overrides) -> dict:
    base = {
        "network": "none", "read_only_rootfs": True, "all_caps_dropped": True, "privileged": False, "no_new_privileges": True,
        "non_root": True, "memory_limit_mb": 256, "cpu_limit": 1.0, "pids_limit": 128, "limits_applied": True, "oom_killed": False,
        "running_when_inspected": True, "exit_code": None, "secret_env_names": 0, "docker_socket_mounted": False,
    }
    return {**base, **overrides}


def host(**overrides) -> dict:
    base = {
        "source": "host", "target": container(), "runner": container(network="target_namespace_only"), "containers_left": 0,
        "wall_s": 12.0, "wall_limit_s": 100.0, "killed_at_wall_limit": False,
    }
    return {**base, **overrides}


def trial(number: int, accepted: int, rejected: int, results: list[str], **overrides) -> dict:
    base = {
        "trial": number, "kind": "concurrent", "status": "completed", "incomplete_reason": None, "planned_requests": 20,
        "completed_requests": accepted + rejected, "peak_in_flight": 20, "accepted": accepted, "rejected": rejected, "unexpected": 0,
        "transport_error": 0, "stock_before": 1, "stock_after": 0, "orders": accepted,
        "invariants": [{"name": name, "result": result} for name, result in zip(INVARIANTS, results)],
    }
    return {**base, **overrides}


def timeline(accepted: int, rejected: int) -> dict:
    points = [{"offset_ms": round(i * 1.1, 1), "duration_ms": 52.0 + i % 9, "outcome": "accepted"} for i in range(accepted)]
    points += [{"offset_ms": round((accepted + i) * 1.1, 1), "duration_ms": 8.0 + i % 5, "outcome": "rejected"} for i in range(rejected)]
    return {"trial": 1, "total_requests": len(points), "truncated": False, "points": points}


def performance() -> dict:
    latency = {"p50": 52.0, "p95": 61.0, "p99": 62.0, "max": 63.0}
    return {
        "observation_only": True, "latency_ms": latency, "latency_ms_by_outcome": {}, "throughput_rps_per_trial": [300.0],
        "error_rate": 0.0, "rejection_rate": 0.0, "status_counts": {"200": 60},
    }


def run_view(**overrides) -> dict:
    base = {
        "target": "ticket-api", "status": "completed", "failure": None, "run_id": "run_20261106_000000_0001", "verdict": "FAIL",
        "verdict_summary": "2 proven violations.",
        "completeness": {"status": "complete", "trials_planned": 3, "trials_completed": 3, "trials_incomplete": 0, "trials_skipped": 0, "serial_control": "completed"},
        "contrast": {
            "trial": 1, "accepted": 20, "rejected": 0, "unexpected": 0, "transport_error": 0, "stock_before": 1, "stock_after": 0,
            "orders": 20, "counts_source": "measured_by_runner", "state_source": "reported_by_target",
        },
        "violations": [
            {"id": "V1", "type": "invariant.capacity_exceeded", "proven": True, "invariant": INVARIANTS[0], "trials_violated": "3/3",
             "observed": {"client.accepted": 20, "before.stock": 1}, "facts": {}},
            {"id": "V2", "type": "invariant.accounting_mismatch", "proven": True, "invariant": INVARIANTS[1], "trials_violated": "3/3",
             "observed": {"after.orders_confirmed": 20}, "facts": {}},
        ],
        "suspected_causes": [{
            "id": "H1", "cause": "check_then_act_race", "status": "suspected", "proven": False, "explains": ["V1"],
            "text": "Requests may check availability and then act, with others getting in between.",
        }],
        "trials": [trial(n, 20, 0, ["fail", "fail", "pass", "pass"]) for n in (1, 2, 3)]
        + [trial(4, 1, 19, ["pass", "pass", "pass", "pass"], kind="serial_control", peak_in_flight=1, orders=1)],
        "timeline": timeline(20, 0), "performance": performance(), "comparison": None, "fix_verified": False,
        "state_verification": {"status": "matched", "mismatches": [], "reason": None},
        "deployed_code_sha256": OLD_HASH, "config_hash": FINGERPRINT, "target_build_id": None, "requests_sent": 79,
        "probes": None, "host_verified": host(),
        "disclosures": ["Payment provider is an in-process stub with a fixed 50 ms delay, which widens the race window.",
                        "Sandboxes are containers, so they share this VM's kernel. gVisor isn't in use."],
    }
    return {**base, **overrides}


def candidate(state: str = "matched", **overrides) -> dict:
    results = ["pass", "pass", "pass", "pass"]
    mismatches = [{"name": "orders_confirmed", "reported": 1, "database": 20}] if state == "mismatch" else []
    comparison = {
        "baseline_run_id": "run_20261106_000000_0001", "status": "COMPARABLE", "fix_verified": True,
        "baseline_build_id": "aaaaaaaa", "candidate_build_id": "bbbbbbbb", "build_ids_self_reported": True,
        "invariants": [
            {"name": INVARIANTS[0], "result": "FIXED", "reason": None},
            {"name": INVARIANTS[1], "result": "FIXED", "reason": None},
            {"name": INVARIANTS[2], "result": "STILL_PASSING", "reason": None},
            {"name": INVARIANTS[3], "result": "STILL_PASSING", "reason": None},
        ],
    }
    base = run_view(
        verdict="PASS", verdict_summary="All invariants held in 3 of 3 complete trials.",
        contrast={**run_view()["contrast"], "accepted": 1, "rejected": 19, "orders": 1},
        violations=[], suspected_causes=[], trials=[trial(n, 1, 19, results, orders=1) for n in (1, 2, 3)],
        timeline=timeline(1, 19), comparison=comparison, fix_verified=True, deployed_code_sha256=NEW_HASH,
        state_verification={"status": state, "mismatches": mismatches, "reason": None},
    )
    return {**base, **overrides}


DIFF = """--- app.py (original)
+++ app.py (candidate)
@@ -10,5 +10,7 @@
 def reserve(request):
-    stock = read_stock()
+    row = conn.execute("UPDATE items SET stock = stock - 1 WHERE stock > 0 RETURNING id").fetchone()
+    if row is None:
+        raise HTTPException(409)
     return confirm(request)"""


def regression_ok() -> list[dict]:
    return [
        {"scenario": name, "passed": True, "verdict": "PASS", "state_verification": "matched", "failure_reason": None, "failed_invariants": []}
        for name in ("plenty-of-stock", "sold-out")
    ]


def attempt(number: int, passed: bool = False, failure: str | None = "candidate_fail", **overrides) -> dict:
    base = {
        "attempt": number, "model": "glm-5.3", "passed": passed, "failure_reason": failure,
        "failure_text": "The fix ran in a new sandbox, and FaultForge still proved a violation." if failure else None,
        "gate_failed": [] if passed else ["comparison_fix_verified"], "policy_violations": [], "verdict": "PASS" if passed else "FAIL",
        "state_verification": "matched", "code_changed": True, "diff": DIFF, "diff_untrusted": True, "diff_truncated": False,
        "input_tokens": 5214, "output_tokens": 688, "usage_reported": True, "cost_usd": 0.0060,
        "candidate": candidate() if passed else run_view(deployed_code_sha256=NEW_HASH),
        "regression": regression_ok() if passed else [],
    }
    return {**base, **overrides}


def rejected_by_policy(number: int) -> dict:
    return attempt(
        number, failure="policy_violation", failure_text="The code broke a code rule, so it was never run.",
        gate_failed=[], policy_violations=["line 12: must not add routes or middleware"], verdict=None, state_verification=None,
        candidate=None,
    )


def fix(status_: str, attempts: list[dict], stop: str | None = None, stop_text: str | None = None) -> dict:
    return {
        "status": status_, "model": "glm-5.3", "reasoning_effort": "low", "max_attempts": 3, "attempts": attempts,
        "stop_reason": stop, "stop_text": stop_text, "total_cost_usd": round(sum(item["cost_usd"] for item in attempts), 6),
    }


def events(*names: str) -> list[dict]:
    return [{"at": f"2026-11-06T21:04:{i * 7:02d}+00:00", "name": name, "attempt": None} for i, name in enumerate(names)]


def run_state(stage: str = "done", result: dict | None = None, fix_: dict | None = None, available: bool = False, position: int | None = None, names: tuple = ()) -> dict:
    return {
        "id": RUN_ID, "replay": False, "target": (result or {}).get("target", "ticket-api"), "stage": stage, "queue_position": position,
        "created_at": "2026-11-06T21:04:00+00:00", "started_at": "2026-11-06T21:04:07+00:00" if stage != "queued" else None,
        "finished_at": None, "events": events(*(names or ("queued", "sandbox_started", "finished"))), "result": result, "fix": fix_,
        "fix_available": available,
    }


def probe(name: str, label: str, result: str, note: str | None = None) -> dict:
    return {"name": name, "label": label, "result": result, "self_reported": True, "note": note}


def hostile_result(**overrides) -> dict:
    base = run_view(
        target="hostile-build", verdict="INCONCLUSIVE", verdict_summary="No proven violation, but 0 of 1 trials completed. This isn't a pass.",
        completeness={"status": "incomplete", "trials_planned": 1, "trials_completed": 0, "trials_incomplete": 1, "trials_skipped": 0, "serial_control": "not_requested"},
        contrast=None, violations=[], suspected_causes=[], timeline=None, performance=None, trials=[],
        state_verification={"status": "not_requested", "mismatches": [], "reason": None}, deployed_code_sha256="e" * 64,
        host_verified=host(target=container(oom_killed=True)),
        probes=[
            probe("egress_external", "Call an external server", "blocked"),
            probe("egress_dns", "Resolve an external name", "blocked"),
            probe("secrets_env", "Search the environment for keys", "blocked"),
            probe("docker_socket", "Reach the Docker socket", "blocked"),
            probe("write_outside_tmp", "Write outside its temporary directory", "blocked"),
            probe("privileges", "Hold Linux capabilities", "blocked"),
            probe("memory", "Exhaust memory", "blocked"),
            probe("fork", "Exhaust processes", "blocked"),
            probe("cpu_spin", "Spin the CPU", "not_blocked", "CPU use is throttled, not stopped. Only the host's wall-clock limit bounds it over time."),
        ],
    )
    return {**base, **overrides}


def replay_summaries() -> list[dict]:
    return [
        {"id": "r_" + "1" * 16, "replay": True, "recorded_at": "2026-11-04T20:22:00+00:00", "target": "ticket-api", "verdict": "FAIL", "fix_verified": True, "has_fix": True},
        {"id": "r_" + "2" * 16, "replay": True, "recorded_at": "2026-11-04T19:57:00+00:00", "target": "hostile-build", "verdict": "INCONCLUSIVE", "fix_verified": False, "has_fix": False},
    ]


def poison(value, field: str | None = None):
    """Puts hostile text into every free string an adapter renders, leaving fixed vocabularies alone
    so that the shape stays valid and the render path is the one a real response takes."""
    free = {"title", "description", "verdict_summary", "message", "text", "label", "note", "stop_text", "failure_text", "model", "diff", "name", "invariant", "id_", "reason"}
    if isinstance(value, dict):
        return {key: poison(item, key) for key, item in value.items()}
    if isinstance(value, list):
        return [poison(item, field) for item in value]
    if isinstance(value, str) and field in free:
        return value + HOSTILE
    if isinstance(value, str) and field == "policy_violations":
        return value + HOSTILE
    if isinstance(value, str) and field == "disclosures":
        return value + HOSTILE
    return value


def states() -> dict:
    """name -> {status, replays, run (GET /api/runs/{id}), replay (GET /api/replays/{id}), post (POST /api/runs answer), click, expect}"""
    replays = replay_summaries()
    base = {"status": status(), "replays": replays}
    verified = fix("verified", [attempt(1), rejected_by_policy(2), attempt(3, True, None)], "fix_verified", "FaultForge verified the fix.")
    exhausted = fix(
        "not_verified", [attempt(1), rejected_by_policy(2), attempt(3)], "attempts_exhausted",
        "No attempt passed, and the agent stopped at its attempt limit.",
    )
    mismatch_attempt = attempt(
        1, False, "state_mismatch", failure_text="What the service reported disagrees with its database.", gate_failed=["state_verification"],
        state_verification="mismatch", verdict="PASS", candidate=candidate("mismatch"),
    )
    failed = run_view(
        status="failed", failure={"reason": "wall_limit_exceeded", "message": "The host killed the run at its wall-clock limit."},
        run_id=None, verdict=None, verdict_summary="The host killed the run at its wall-clock limit.", completeness=None, contrast=None,
        violations=[], suspected_causes=[], trials=[], timeline=None, performance=None, deployed_code_sha256=None, config_hash=None,
        state_verification={"status": "unavailable", "mismatches": [], "reason": "no_report"}, requests_sent=None,
        host_verified=host(target=None, runner=None, containers_left=None, killed_at_wall_limit=True),
    )
    inconclusive = run_view(
        verdict="INCONCLUSIVE", verdict_summary="No proven violation, but 1 of 3 trials completed. This isn't a pass.",
        completeness={"status": "incomplete", "trials_planned": 3, "trials_completed": 1, "trials_incomplete": 1, "trials_skipped": 1, "serial_control": "skipped"},
        violations=[], suspected_causes=[], timeline=None,
        trials=[
            trial(1, 1, 19, ["pass", "pass", "pass", "pass"]),
            trial(2, 0, 0, [], status="incomplete", incomplete_reason="timeout"),
            trial(3, 0, 0, [], status="skipped", incomplete_reason="budget_exhausted"),
        ],
    )
    replay = {
        "id": "r_" + "1" * 16, "replay": True, "recorded_at": "2026-11-04T20:22:00+00:00", "target": "ticket-api",
        "result": run_view(), "fix": verified,
    }
    regression_broke = attempt(
        1, False, "regression_failed", failure_text="The fix passed the main scenario, but broke another one.", gate_failed=["regression_suite"], verdict="PASS", candidate=candidate(),
        regression=[
            {"scenario": "plenty-of-stock", "passed": False, "verdict": "FAIL", "state_verification": "matched", "failure_reason": "candidate_fail", "failed_invariants": ["Every buyer gets a ticket while stock lasts"]},
            {"scenario": "sold-out", "passed": True, "verdict": "PASS", "state_verification": "matched", "failure_reason": None, "failed_invariants": []},
        ],
    )
    budget = copy.deepcopy(status(agent_enabled=False, agent_disabled_reason="daily_usd_cap"))
    budget["budget"]["exhausted"] = True
    result = {
        "start": {**base},
        "no-replays": {"status": status(replays_available=0), "replays": []},
        "busy": {**base, "status": status(busy=True, queue_length=2)},
        "fix-limit": {
            **base, "status": status(agent_enabled=False, agent_disabled_reason="fix_ip_limit"),
            "run": run_state(result=run_view()),
        },
        "queue-full": {**base, "status": status(busy=True, queue_length=3)},
        "cooldown": {**base, "post": {"code": 429, "retry_after": 25, "body": {"reason": "cooldown", "message": "Please wait before starting another run."}}},
        "budget": {**base, "status": budget},
        "queued": {**base, "run": run_state("queued", position=2, names=("queued",))},
        "running": {**base, "run": run_state("running", names=("queued", "sandbox_started"))},
        "fail": {**base, "run": run_state(result=run_view(), available=True)},
        "agent-disabled": {**base, "status": status(agent_enabled=False, agent_disabled_reason="not_enabled"), "run": run_state(result=run_view())},
        "agent-running": {**base, "run": run_state("fixing", run_view(), fix("running", []), names=("queued", "sandbox_started", "finished", "fix_queued", "fix_started"))},
        "rejected": {**base, "run": run_state("fixing", run_view(), fix("running", [attempt(1), rejected_by_policy(2)]), names=("queued", "sandbox_started", "finished", "fix_queued", "fix_started"))},
        "verified": {**base, "run": run_state(result=run_view(), fix_=verified)},
        "pass-mismatch": {**base, "run": run_state(result=run_view(), fix_=fix("not_verified", [mismatch_attempt], "attempts_exhausted", "No attempt passed, and the agent stopped at its attempt limit."))},
        "pass-alone-mismatch": {**base, "run": run_state(result=candidate("mismatch", comparison=None, fix_verified=False))},
        "honest": {**base, "run": run_state(result=run_view(), fix_=exhausted)},
        "regression-broke": {**base, "run": run_state(result=run_view(), fix_=fix("not_verified", [regression_broke], "attempts_exhausted", "No attempt passed, and the agent stopped at its attempt limit."))},
        "inconclusive": {**base, "run": run_state(result=inconclusive)},
        "hostile": {**base, "run": run_state(result=hostile_result())},
        "failed": {**base, "run": run_state("failed", failed, names=("queued", "sandbox_started", "failed"))},
        "replay": {**base, "replay": replay},
        "replay-budget": {**base, "status": budget, "replay": replay},
    }
    result["hostile-strings"] = {
        "status": poison(status()), "replays": replays,
        "run": poison(run_state(result=run_view(), fix_=verified)),
    }
    result["hostile-strings-probes"] = {
        "status": poison(status()), "replays": replays, "run": poison(run_state(result=hostile_result())),
    }
    return result
