"""Host-side tests for the hostile fixture: the guard, the parsing and the output format only.

Nothing here may run an attack. The module is imported with the guard failing, and tripwires
on the dangerous primitives prove it stays that way.
"""

import asyncio
import errno
import importlib.util
import json
import os
import signal
import socket
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import httpx
import pytest

from faultforge.config import find_scenario, load_config
from faultforge.expressions import parse

HOSTILE_DIR = Path(__file__).resolve().parents[1] / "demo" / "hostile-build"
EMPTY_ROUTES = "Iface\tDestination\tGateway\tFlags\tRefCnt\tUse\tMetric\tMask\tMTU\tWindow\tIRTT\n"
ONE_ROUTE = EMPTY_ROUTES + "eth0\t00000000\t0100A8C0\t0003\t0\t0\t0\t00000000\t0\t0\t0\n"


@pytest.fixture
def tripwires(monkeypatch) -> list[str]:
    tripped: list[str] = []

    def wire(owner: object, name: str) -> None:
        def fire(*args: object, **kwargs: object) -> None:
            tripped.append(name)
            raise AssertionError(f"{name} was called")

        monkeypatch.setattr(owner, name, fire)

    wire(os, "fork")
    wire(os, "setuid")
    wire(subprocess, "Popen")
    wire(socket, "create_connection")
    wire(socket, "getaddrinfo")
    return tripped


@pytest.fixture
def hostile(monkeypatch, tripwires) -> ModuleType:
    monkeypatch.delenv("FF_HOSTILE_ALLOWED", raising=False)
    monkeypatch.setattr(sys, "dont_write_bytecode", True)
    spec = importlib.util.spec_from_file_location("ff_hostile_app", HOSTILE_DIR / "app.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def probe_lines(text: str) -> list[dict]:
    return [json.loads(line.removeprefix("FF_PROBE ")) for line in text.splitlines() if line.startswith("FF_PROBE ")]


# The guard


@pytest.mark.parametrize(
    "allowed, dockerenv, routes, expected",
    [
        (None, True, EMPTY_ROUTES, "FF_HOSTILE_ALLOWED"),
        ("0", True, EMPTY_ROUTES, "FF_HOSTILE_ALLOWED"),
        ("true", True, EMPTY_ROUTES, "FF_HOSTILE_ALLOWED"),
        ("1", False, EMPTY_ROUTES, "/.dockerenv"),
        ("1", True, ONE_ROUTE, "routes exist"),
        ("1", True, None, "unreadable"),
        ("1", True, "", "malformed"),
        ("1", True, "garbage\n", "malformed"),
        (None, False, ONE_ROUTE, "FF_HOSTILE_ALLOWED"),
        (None, False, ONE_ROUTE, "/.dockerenv"),
        (None, False, ONE_ROUTE, "routes exist"),
    ],
)
def test_guard_refuses_each_failing_combination(hostile, allowed, dockerenv, routes, expected):
    refusal = hostile.guard_refusal(allowed, dockerenv, routes)
    assert refusal is not None and refusal.startswith("refused: ") and expected in refusal


def test_guard_allows_only_when_every_condition_holds(hostile):
    assert hostile.guard_refusal("1", True, EMPTY_ROUTES) is None


@pytest.mark.skipif(Path("/.dockerenv").exists(), reason="only meaningful outside a container")
def test_opting_in_is_not_enough_on_the_host(hostile, monkeypatch):
    monkeypatch.setenv("FF_HOSTILE_ALLOWED", "1")
    assert hostile.current_refusal() is not None


# Nothing runs without the guard


def test_import_refuses_and_runs_nothing(capsys, hostile, tripwires):
    lines = probe_lines(capsys.readouterr().out)
    assert [(line["probe"], line["phase"], line["outcome"]) for line in lines] == [("guard", "result", "inconclusive")]
    assert lines[0]["detail"].startswith("refused: ")
    assert tripwires == []


def test_run_attacks_refuses_before_any_probe(hostile, tripwires, monkeypatch, capsys):
    called = []
    monkeypatch.setattr(hostile, "PROBES", [("trap", lambda: called.append(1) or ("blocked", ""))])
    capsys.readouterr()
    hostile.run_attacks()
    assert called == [] and tripwires == []
    assert [line["probe"] for line in probe_lines(capsys.readouterr().out)] == ["guard"]


def test_no_child_process_starts_without_the_guard(hostile, tripwires):
    with pytest.raises(hostile.GuardRefused):
        hostile._run_child("pass", 1.0)
    assert tripwires == []


# The target contract, with the guard refusing


def test_hooks_match_the_contract(hostile):
    async def scenario() -> tuple[int, dict, int, dict]:
        transport = httpx.ASGITransport(app=hostile.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            health = await client.get("/health")
            reset = await client.post("/__ff/reset", json={"stock": 7})
            state = await client.get("/__ff/state")
            return health.status_code, reset.json(), state.status_code, state.json()

    health, reset, status, state = asyncio.run(scenario())
    assert health == 200 and status == 200 and reset["stock"] == 7
    assert state["stock"] == 7
    for field in ("orders_confirmed", "payments_unattached", "orders_without_payment", "in_flight"):
        assert type(state[field]) is int
    assert state["stale_source"] is False
    assert set(state["build"]) == {"build_id", "instance_id", "instance_started_at", "stale_source"}
    assert state["build"]["stale_source"] is False
    assert len(state["build"]["instance_id"]) == 32


def test_reserve_hangs_and_attacks_are_refused(hostile, tripwires, capsys):
    async def scenario() -> int:
        transport = httpx.ASGITransport(app=hostile.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(client.post("/reserve", json={"item_id": 1}), 0.5)
            return (await client.get("/__ff/state")).json()["in_flight"]

    assert asyncio.run(scenario()) == 0
    hostile._attacks_thread.join(5)
    assert tripwires == []
    lines = probe_lines(capsys.readouterr().out)
    assert {line["probe"] for line in lines} == {"guard"}


# Output format


def test_probe_line_format_and_length(hostile):
    line = hostile.probe_line("memory", "result", "blocked", "x" * 5000 + '"\\\né' * 5000)
    assert line.startswith("FF_PROBE ")
    assert len(line.encode()) < 512
    assert "\n" not in line
    fields = json.loads(line.removeprefix("FF_PROBE "))
    assert set(fields) == {"probe", "phase", "outcome", "detail"}


def test_probe_line_stays_short_when_every_character_needs_escaping(hostile):
    assert len(hostile.probe_line("egress_external", "result", "inconclusive", '"' * 500).encode()) < 512


# Parsing and the evidence behind "blocked"


def test_secret_scan_reports_names_and_never_values(hostile):
    blob = b"API_KEY=hunter2\0PATH=/usr/bin\0db_password=swordfish\0HARMLESS=TOKEN\0noequals\0"
    names = hostile.environ_names(blob)
    assert names == ["API_KEY", "PATH", "db_password", "HARMLESS"]
    assert hostile.secret_names(names) == ["API_KEY", "db_password"]
    assert not any(b"hunter2" in name.encode() or b"swordfish" in name.encode() for name in names)


@pytest.mark.parametrize(
    "error, outcome",
    [
        (None, "not_blocked"),
        (OSError(errno.ENETUNREACH, "unreachable"), "blocked"),
        (OSError(errno.EPERM, "denied"), "blocked"),
        (TimeoutError(), "blocked"),
        (ConnectionRefusedError(errno.ECONNREFUSED, "refused"), "inconclusive"),
    ],
)
def test_connect_outcome(hostile, error, outcome):
    assert hostile.connect_outcome(error)[0] == outcome


@pytest.mark.parametrize(
    "cap, error, outcome",
    [
        ("0000000000000000", PermissionError(errno.EPERM, "no"), "blocked"),
        ("0000000000000000", None, "not_blocked"),
        ("00000000a80425fb", PermissionError(errno.EPERM, "no"), "not_blocked"),
        (None, PermissionError(errno.EPERM, "no"), "inconclusive"),
    ],
)
def test_privileges_outcome(hostile, cap, error, outcome):
    assert hostile.privileges_outcome(cap, error)[0] == outcome


@pytest.mark.parametrize(
    "returncode, output, oom_kills, outcome",
    [
        (-signal.SIGKILL, "16\n32\n", 1, "blocked"),
        (-signal.SIGKILL, "16\n32\n", None, "blocked"),
        (-signal.SIGKILL, "16\n32\n", 0, "inconclusive"),
        (3, "16\nmemoryerror\n", None, "blocked"),
        (0, "4096\n", None, "not_blocked"),
        (None, "16\n", None, "inconclusive"),
        (1, "", None, "inconclusive"),
    ],
)
def test_memory_outcome(hostile, returncode, output, oom_kills, outcome):
    assert hostile.memory_outcome(returncode, output, oom_kills)[0] == outcome


@pytest.mark.parametrize(
    "returncode, output, outcome",
    [
        (0, '{"count": 124, "reaped": 124, "error": "BlockingIOError"}', "blocked"),
        (0, '{"count": 2000, "reaped": 2000, "error": "cap"}', "not_blocked"),
        (0, '{"count": 5, "reaped": 5, "error": "deadline"}', "inconclusive"),
        (-9, "", "inconclusive"),
        (None, "", "inconclusive"),
    ],
)
def test_fork_outcome(hostile, returncode, output, outcome):
    assert hostile.fork_outcome(returncode, output)[0] == outcome


@pytest.mark.parametrize(
    "returncode, output, throttled, outcome",
    [
        (0, "10.0 9.9", 0, "not_blocked"),
        (0, "10.0 9.9", 5, "not_blocked"),
        (0, "10.0 2.0", 40, "blocked"),
        (0, "10.0 2.0", None, "not_blocked"),
        (-9, "", None, "blocked"),
        (None, "", None, "inconclusive"),
        (0, "", 0, "inconclusive"),
    ],
)
def test_cpu_outcome(hostile, returncode, output, throttled, outcome):
    assert hostile.cpu_outcome(returncode, output, throttled)[0] == outcome


# The scenario


def test_config_is_valid_for_faultforge():
    config = load_config(HOSTILE_DIR)
    scenario = find_scenario(config, "hostile-build")
    assert scenario.mutates_data is True and scenario.user_confirmed is True
    assert (scenario.load.vus, scenario.load.iterations, scenario.load.max_duration_s) == (5, 5, 6)
    assert (scenario.request.method, scenario.request.path) == ("POST", "/reserve")
    assert config.target.state == "/__ff/state" and config.target.reset.path == "/__ff/reset"
    checks = {invariant.check: invariant.type for invariant in scenario.invariants}
    assert checks["client.accepted <= before.stock"] == "capacity"
    assert any(
        any(variable.startswith("after.") for variable in parse(check).variables) for check in checks
    )
    assert any("intentionally hostile" in disclosure for disclosure in scenario.disclosures)
