"""M4 acceptance: real containers, real k6. Removes only containers carrying the sandbox label."""

import ast
import dataclasses
import importlib.util
import json
import os
import secrets
import shutil
import socket
import sqlite3
import subprocess
import sys
import threading
import time
import tomllib
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from faultforge import lifecycle, sandbox
from faultforge.cli import main as cli_main
from faultforge.errors import FaultForgeError
from faultforge.lifecycle import RunOptions
from faultforge.limits import LIMITS
from faultforge.models import TargetConfig
from faultforge.sandbox import (
    DEFAULT_POLICY,
    EXPIRES_LABEL,
    LABEL,
    ROLE_LABEL,
    TICKET_API_STATE_VERIFIER,
    SandboxResult,
    TargetSpec,
    run_sandboxed,
)

ROOT = Path(__file__).resolve().parents[1]
TICKET_API = ROOT / "demo" / "ticket-api"
HOSTILE = ROOT / "demo" / "hostile-build"
SUPPORT = TICKET_API / "ticket_support"
TICKET_CONFIG = TICKET_API / "faultforge.yaml"
BUGGY = (TICKET_API / "app.py").read_bytes()
REFERENCE_FIX = (TICKET_API / "reference_fix" / "app.py").read_bytes()
SCENARIO = "last-ticket-rush"
FAST = RunOptions(trials=1, serial_control=False)
CANARY = "canary-not-a-real-key-7f3a"
KEY_NAME = "VULTR_INFERENCE_API_KEY"
TARGET_ENV_NAMES = ["FF_DB_PATH", "FF_TEST_HOOKS", "PATH", "PYTHONDONTWRITEBYTECODE", "PYTHONPATH"]
RUNNER_ENV_NAMES = ["HOME", "PATH", "PYTHONDONTWRITEBYTECODE"]

MINI_CONFIG = """
version: 1
target:
  origin: http://127.0.0.1:8000
  health: /health
  reset: { method: POST, path: /__ff/reset, json: { stock: 1000 } }
  state: /__ff/state
scenarios:
  - id: mini
    description: a small test target
    mutates_data: true
    user_confirmed: true
    script: burst_request
    request: { method: POST, path: /reserve, json: { item_id: 1 } }
    outcomes:
      accepted_status: [200]
      rejected_status: [409]
      capture_field: order_id
    load: { vus: 4, iterations: 4, max_duration_s: 15 }
    invariants:
      - name: Not oversold
        type: capacity
        check: client.accepted <= before.stock
"""

MINI_APP = """
import asyncio, os, time
from fastapi import FastAPI
from fastapi.responses import StreamingResponse

{startup}
app = FastAPI()
stock = 0
BUILD = {{"build_id": "mini", "instance_id": "i-" + str(os.getpid()), "instance_started_at": "t", "stale_source": False}}

def snapshot():
    return {{"stock": stock, "in_flight": 0, "stale_source": False, "build": BUILD}}

@app.get("/health")
async def health():
    return {{"status": "ok"}}

@app.post("/__ff/reset")
async def reset(body: dict):
    global stock
    stock = body["stock"]
    return snapshot()

@app.get("/__ff/state")
async def state():
    return snapshot()

@app.post("/reserve")
async def reserve():
{reserve}
"""
FAST_RESERVE = '    await asyncio.sleep(0.05)\n    return {"order_id": "o"}'
RELEASE_FILE = "/tmp/release"
GATED_RESERVE = f"""    while not os.path.exists("{RELEASE_FILE}"):
        await asyncio.sleep(0.05)
    return {{"order_id": "o"}}"""
HANG_RESERVE = "    await asyncio.sleep(3600)"
BODY_MB = 64
MEMORY_OVERSHOOT_BYTES = 32 * 2**20
STREAM_RESERVE = f"""    async def body():
        yield b'{{"order_id":"'
        chunk = b"x" * (1024 * 1024)
        for _ in range({BODY_MB}):
            yield chunk
        yield b'"}}'
    return StreamingResponse(body(), media_type="application/json")"""

docker_test = pytest.mark.docker


def mini_app(reserve: str = FAST_RESERVE, startup: str = "") -> bytes:
    return MINI_APP.format(reserve=reserve, startup=startup).encode()


def mini_spec(tmp_path: Path, app: bytes) -> TargetSpec:
    config = tmp_path / "mini.yaml"
    config.write_text(MINI_CONFIG, encoding="utf-8")
    return TargetSpec(app_source=app, config_path=config)


def ticket_spec(app: bytes) -> TargetSpec:
    return TargetSpec(app_source=app, support_dir=SUPPORT, config_path=TICKET_CONFIG)


def docker(*args: str, timeout: float = 60) -> subprocess.CompletedProcess:
    return subprocess.run(["docker", *args], capture_output=True, text=True, timeout=timeout, check=False)


def labelled_ids() -> set[str]:
    return set(docker("ps", "-aq", "--no-trunc", "--filter", f"label={LABEL}").stdout.split())


def unlabelled_ids() -> set[str]:
    """IDs only: nothing about the owner's other containers is inspected."""
    return set(docker("ps", "-aq", "--no-trunc").stdout.split()) - labelled_ids()


def report_files(project: Path) -> list[Path]:
    return list(project.glob(".faultforge/runs/*/report.json"))


def assert_failed(result: SandboxResult, project: Path, *reasons: str) -> None:
    assert result.status == "failed"
    assert result.failure_reason in reasons, result.failure_detail
    assert result.report is None
    assert report_files(project) == []


@pytest.fixture(scope="session")
def images() -> Iterator[None]:
    """Always runs the build. It's cached, and an image from older source would test the wrong code."""
    available = docker("version", "--format", "{{.Server.Version}}")
    if available.returncode != 0:
        pytest.fail(f"docker tests need Docker; deselect with -m 'not docker': {available.stderr}")
    built = subprocess.run(
        ["bash", str(ROOT / "scripts" / "build_images.sh")], capture_output=True, text=True, timeout=600, check=False
    )
    assert built.returncode == 0, built.stderr
    others = unlabelled_ids()
    yield
    assert unlabelled_ids() == others, "the owner's other containers changed during the session"


@pytest.fixture
def contained(images: None) -> Iterator[None]:
    """Every docker test ends with no labelled containers, and the owner's others untouched."""
    others = unlabelled_ids()
    assert labelled_ids() == set()
    yield
    assert labelled_ids() == set()
    assert unlabelled_ids() == others


def show(capsys: pytest.CaptureFixture, message: str) -> None:
    with capsys.disabled():
        print(f"\n[sandbox] {message}")


class CgroupWatcher:
    """Samples the runner's cgroup from inside it, since the numbers go when it's removed."""

    def __init__(self) -> None:
        self.peak_bytes = 0
        self.oom_kills = 0
        self.pids_peak = 0
        self.samples = 0
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def __enter__(self) -> "CgroupWatcher":
        self._thread.start()
        return self

    def __exit__(self, *_: object) -> None:
        self._stop.set()
        self._thread.join(timeout=30)

    def _run(self) -> None:
        while not self._stop.is_set():
            ids = docker("ps", "-q", "--filter", f"label={ROLE_LABEL}=runner").stdout.split()
            if ids:
                read = docker(
                    "exec", ids[0], "cat", "/sys/fs/cgroup/memory.peak", "/sys/fs/cgroup/pids.peak",
                    "/sys/fs/cgroup/memory.events",
                )
                if read.returncode == 0:
                    fields = read.stdout.split()
                    self.peak_bytes = max(self.peak_bytes, int(fields[0]))
                    self.pids_peak = max(self.pids_peak, int(fields[1]))
                    self.oom_kills = max(self.oom_kills, int(fields[fields.index("oom_kill") + 1]))
                    self.samples += 1
            self._stop.wait(0.1)


# No containers needed


@pytest.mark.parametrize("name", ["target", "runner"])
def test_pins_match_the_lock(name: str) -> None:
    lock = tomllib.loads((ROOT / "uv.lock").read_text(encoding="utf-8"))
    locked = {package["name"]: package["version"] for package in lock["package"]}
    lines = (ROOT / "docker" / f"{name}-requirements.txt").read_text(encoding="utf-8").splitlines()
    pins = [line.split("==") for line in lines if line and not line.startswith("#")]
    assert pins
    for package, version in pins:
        assert locked[package] == version, package


def test_target_command_is_contained(tmp_path: Path) -> None:
    argv = sandbox.target_argv("t", "sb-1", tmp_path, False, DEFAULT_POLICY)
    joined = " ".join(argv)
    for expected in (
        "--pull never", "--network none", "--user 65534:65534", "--read-only", "--cap-drop ALL",
        "--security-opt no-new-privileges", "--ipc none", "--memory 256m", "--memory-swap 256m",
        "--cpus 1", "--pids-limit 128", f"--label {LABEL}=sb-1", "--host 127.0.0.1", "--workers 1",
        f"type=bind,src={tmp_path},dst=/app,readonly",
    ):
        assert expected in joined
    assert "docker.sock" not in joined
    env_values = [argv[index + 1] for index, item in enumerate(argv) if item == "--env"]
    assert sorted(value.partition("=")[0] for value in env_values) == sorted(sandbox.target_env(False))
    assert "FF_HOSTILE_ALLOWED" not in joined
    assert "FF_HOSTILE_ALLOWED=1" in sandbox.target_argv("t", "sb-1", tmp_path, True, DEFAULT_POLICY)


def test_runner_command_is_contained(tmp_path: Path) -> None:
    options = RunOptions(trials=2, serial_control=False, compare_to="run_20260101_000000_abcd")
    argv = sandbox.runner_argv("r", "sb-1", "t", tmp_path, "mini", options, DEFAULT_POLICY)
    joined = " ".join(argv)
    for expected in (
        "--network container:t", f"--user {os.getuid()}:{os.getgid()}", "--read-only", "--cap-drop ALL",
        "--memory 512m", "--memory-swap 512m", "--cpus 2", "--pids-limit 512",
        f"type=bind,src={tmp_path},dst=/out", "--project /out run mini --json",
        "--trials=2", "--no-serial-control", "--compare-to=run_20260101_000000_abcd",
    ):
        assert expected in joined


def test_wall_limit_covers_every_budget() -> None:
    assert DEFAULT_POLICY.wall_limit_s > LIMITS.max_wall_s_total + lifecycle.MAX_WAIT_FOR_TARGET_S
    assert DEFAULT_POLICY.stale_after_s > DEFAULT_POLICY.wall_limit_s
    with pytest.raises(dataclasses.FrozenInstanceError):
        DEFAULT_POLICY.wall_limit_s = 1  # type: ignore[misc]


def test_docker_gets_a_minimal_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(KEY_NAME, CANARY)
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", CANARY)
    monkeypatch.setenv("DOCKER_CONTEXT", "default")
    env = sandbox.docker_env()
    assert CANARY not in env.values()
    assert KEY_NAME not in env
    assert env["DOCKER_CONTEXT"] == "default"


def test_a_root_host_user_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sandbox.os, "getuid", lambda: 0)
    result = run_sandboxed(mini_spec(tmp_path, mini_app()), tmp_path / "project", "mini", FAST)
    assert result.status == "failed" and result.failure_reason == "invalid_spec"
    assert "root" in (result.failure_detail or "")


def test_probes_are_bounded_and_self_reported() -> None:
    lines = [
        b'FF_PROBE {"probe": "a", "phase": "result", "self_reported": false}',
        b"FF_PROBE not json",
        b"FF_PROBE [1, 2]",
        b"FF_PROBE NaN",
        b'FF_PROBE {"bad": NaN}',
        b'FF_PROBE {"surrogate": "\\ud800"}',
        b'FF_PROBE {"long": "' + b"x" * 600 + b'"}',
        b'INFO FF_PROBE {"not": "at the start"}',
    ]
    probes = sandbox.parse_probes(b"\n".join(lines))
    assert probes == [{"probe": "a", "phase": "result", "self_reported": True}]
    many = sandbox.parse_probes(b'FF_PROBE {"n": 1}\n' * 500)
    assert len(many) == sandbox.MAX_PROBES
    assert all(probe["self_reported"] is True for probe in many)


def test_a_started_probe_with_no_result_counts_as_unfinished() -> None:
    probes = sandbox.parse_probes(
        b'FF_PROBE {"probe": "a", "phase": "started"}\nFF_PROBE {"probe": "a", "phase": "result"}\n'
        b'FF_PROBE {"probe": "b", "phase": "started"}\n'
    )
    assert sandbox.unfinished_probes(probes) == ["b"]


def test_code_dir_has_no_bytecode_and_is_readable(tmp_path: Path) -> None:
    support = tmp_path / "support_pkg"
    (support / "__pycache__").mkdir(parents=True)
    (support / "__pycache__" / "x.cpython-312.pyc").write_bytes(b"stale")
    (support / "stray.pyc").write_bytes(b"stale")
    (support / "mod.py").write_text("x = 1\n", encoding="utf-8")
    os.chmod(support / "mod.py", 0o600)
    code = sandbox.write_code_dir(TargetSpec(app_source=b"app", config_path=tmp_path, support_dir=support))
    try:
        assert sorted(path.name for path in code.rglob("*")) == ["app.py", "mod.py", "support_pkg"]
        for path in [code, *code.rglob("*")]:
            assert path.stat().st_mode & 0o005 == 0o005 if path.is_dir() else path.stat().st_mode & 0o004
    finally:
        sandbox.shutil.rmtree(code)


@pytest.mark.parametrize("link_name", ["evil.py", "evil_dir"])
def test_symlinks_are_refused(tmp_path: Path, link_name: str) -> None:
    support = tmp_path / "support_pkg"
    support.mkdir()
    (support / link_name).symlink_to(tmp_path)
    with pytest.raises(sandbox.SandboxError) as caught:
        sandbox.write_code_dir(TargetSpec(app_source=b"app", config_path=tmp_path, support_dir=support))
    assert caught.value.reason == "invalid_spec"


def test_a_symlinked_support_dir_is_refused(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "linked"
    link.symlink_to(real)
    result = run_sandboxed(
        TargetSpec(app_source=b"app", config_path=TICKET_CONFIG, support_dir=link), tmp_path / "p", SCENARIO, FAST
    )
    assert result.status == "failed" and result.failure_reason == "invalid_spec"


@pytest.mark.parametrize(
    "change",
    [{"scenario_id": "unknown"}, {"options": RunOptions(compare_to="../../etc/passwd")}],
)
def test_bad_requests_never_start_a_container(tmp_path: Path, change: dict) -> None:
    fake = tmp_path / "docker-must-not-run"
    arguments = {"scenario_id": SCENARIO, "options": FAST, **change}
    result = run_sandboxed(
        ticket_spec(BUGGY), tmp_path / "project", policy=dataclasses.replace(DEFAULT_POLICY, docker=str(fake)), **arguments
    )
    assert result.status == "failed" and result.failure_reason == "invalid_spec"


def test_a_config_for_another_origin_is_refused(tmp_path: Path) -> None:
    config = tmp_path / "other.yaml"
    config.write_text(MINI_CONFIG.replace("127.0.0.1:8000", "127.0.0.1:9000"), encoding="utf-8")
    result = run_sandboxed(TargetSpec(app_source=b"x", config_path=config), tmp_path / "p", "mini", FAST)
    assert result.status == "failed" and result.failure_reason == "invalid_spec"


def test_wait_for_target_is_capped(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(lifecycle, "MAX_WAIT_FOR_TARGET_S", 0.3)
    target = TargetConfig(origin="http://127.0.0.1:9", health="/health", state="/state")
    started = time.monotonic()
    lifecycle.wait_for_target(target, 1000, LIMITS)
    assert 0.25 < time.monotonic() - started < 2


@pytest.mark.parametrize("value", ["-1", "nan"])
def test_wait_for_target_rejects_nonsense(tmp_path: Path, value: str, capsys: pytest.CaptureFixture) -> None:
    code = cli_main(["--project", str(tmp_path), "run", "x", f"--wait-for-target-s={value}"])
    assert code == 3
    assert json.loads(capsys.readouterr().out)["error_class"] == "config_invalid"
    with pytest.raises(FaultForgeError):
        lifecycle.run_scenario(tmp_path, "x", roots=[tmp_path], wait_for_target_s=float(value))


# 1, 2: the product path


@docker_test
def test_buggy_fails_and_the_fix_is_verified_in_new_sandboxes(
    contained: None, tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    project = tmp_path / "project"
    with CgroupWatcher() as watcher:
        buggy = run_sandboxed(ticket_spec(BUGGY), project, SCENARIO)
    assert buggy.status == "completed", buggy.failure_detail
    assert buggy.report.verdict == "FAIL"
    assert buggy.containment.containers_left == 0

    fixed = run_sandboxed(ticket_spec(REFERENCE_FIX), project, SCENARIO, RunOptions(compare_to=buggy.report.run_id))
    assert fixed.status == "completed", fixed.failure_detail
    assert fixed.report.verdict == "PASS"
    assert fixed.report.comparison.fix_verified is True
    assert fixed.report.target.build.build_id != buggy.report.target.build.build_id
    assert fixed.report.target.build.stale_source is False
    assert fixed.containment.target.container_id != buggy.containment.target.container_id
    assert len(report_files(project)) == 2

    show(
        capsys,
        f"buggy {buggy.containment.wall_s} s, fixed {fixed.containment.wall_s} s (3 trials plus serial control each); "
        f"runner memory peak {watcher.peak_bytes / 2**20:.0f} MiB of {DEFAULT_POLICY.runner.memory_mb}, "
        f"samples {watcher.samples}, oom kills {watcher.oom_kills}",
    )
    assert watcher.oom_kills == 0


@docker_test
def test_a_same_size_edit_changes_the_build_and_the_same_source_does_not(contained: None, tmp_path: Path) -> None:
    edited = BUGGY.replace(b'"sold out"', b'"SOLD OUT"')
    assert edited != BUGGY and len(edited) == len(BUGGY)
    project = tmp_path / "project"
    first = run_sandboxed(ticket_spec(BUGGY), project, SCENARIO, FAST)
    second = run_sandboxed(ticket_spec(edited), project, SCENARIO, FAST)
    third = run_sandboxed(ticket_spec(BUGGY), project, SCENARIO, FAST)
    assert first.report and second.report and third.report
    assert first.report.target.build.build_id != second.report.target.build.build_id
    assert first.report.target.build.build_id == third.report.target.build.build_id
    assert first.report.target.build.instance_id != third.report.target.build.instance_id


@docker_test
def test_waiting_for_the_target_is_outside_the_run_budget(contained: None, tmp_path: Path) -> None:
    app = mini_app(startup="time.sleep(3)")
    result = run_sandboxed(mini_spec(tmp_path, app), tmp_path / "project", "mini", FAST)
    assert result.status == "completed", result.failure_detail
    assert result.report.verdict == "PASS"
    assert result.containment.wall_s >= 3
    assert result.report.budget.wall_s_used < result.containment.wall_s - 2.5


# 3, 4: egress, files and secrets, checked from inside containers that are running


class Listener:
    def __init__(self, host: str) -> None:
        self.hits = 0
        self.socket = socket.create_server((host, 0))
        self.socket.settimeout(0.1)
        self.port = self.socket.getsockname()[1]
        self.host = host
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                connection, _ = self.socket.accept()
            except TimeoutError:
                continue
            self.hits += 1
            connection.close()

    def close(self) -> None:
        self._stop.set()
        self._thread.join(timeout=5)
        self.socket.close()


GATEWAY_LISTENER_SCRIPT = """
import socket, sys
server = socket.create_server((sys.argv[1], 0))
print("PORT", server.getsockname()[1], flush=True)
while True:
    connection, _ = server.accept()
    print("HIT", flush=True)
    connection.close()
"""


class GatewayListener:
    """Listens on the bridge gateway, wherever that address lives. With Docker Desktop it's inside
    Docker's VM and this machine can't bind it, so a labelled helper on that VM's host network does."""

    def __init__(self, gateway: str) -> None:
        self.host = gateway
        self.helper = ""
        self.direct: Listener | None = None
        try:
            self.direct = Listener(gateway)
            self.port = self.direct.port
            return
        except OSError:
            pass
        started = docker(
            "run", "-d", "--pull", "never", "--label", f"{LABEL}=test-listener", "--network", "host",
            "--read-only", "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
            "faultforge-target:dev", "python", "-c", GATEWAY_LISTENER_SCRIPT, gateway,
        )
        assert started.returncode == 0, started.stderr
        self.helper = started.stdout.strip()
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            words = docker("logs", self.helper).stdout.split()
            if "PORT" in words:
                self.port = int(words[words.index("PORT") + 1])
                return
            time.sleep(0.05)
        self.close()
        pytest.fail("the gateway listener didn't start")

    @property
    def hits(self) -> int:
        if self.direct is not None:
            return self.direct.hits
        return docker("logs", self.helper).stdout.split().count("HIT")

    def close(self) -> None:
        if self.direct is not None:
            self.direct.close()
        if self.helper:
            docker("rm", "-f", self.helper)


def primary_host_address() -> str:
    """Sends nothing: connecting a UDP socket only chooses the route."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
        probe.connect(("1.1.1.1", 9))
        return probe.getsockname()[0]


CONNECT_SCRIPT = """
import json, socket, sys
results = {}
for host, port in json.loads(sys.argv[1]):
    try:
        socket.create_connection((host, port), timeout=3).close()
        results[f"{host}:{port}"] = "connected"
    except OSError as error:
        results[f"{host}:{port}"] = type(error).__name__ + " " + str(error.errno)
try:
    socket.getaddrinfo("example.com", 443)
    results["dns"] = "resolved"
except OSError as error:
    results["dns"] = type(error).__name__
print(json.dumps(results))
"""
FILE_CHECK_SCRIPT = """
id -u
test -e /out && echo out-visible || echo out-absent
test -e /dev/shm && echo shm-present || echo shm-absent
test -e /var/run/docker.sock && echo socket-present || echo socket-absent
touch /app/x 2>/dev/null && echo app-writable || echo app-readonly
touch /usr/x 2>/dev/null && echo root-writable || echo root-readonly
touch /tmp/ok && echo tmp-writable || echo tmp-readonly
grep CapEff /proc/self/status
wc -l < /proc/net/route
"""


@dataclass
class LiveCheck:
    result: SandboxResult | None = None
    project: Path | None = None
    target_id: str = ""
    runner_id: str = ""
    both_running: bool = False
    gateway: str = ""
    connects: dict[str, dict[str, str]] = field(default_factory=dict)
    files: dict[str, list[str]] = field(default_factory=dict)
    inspected: dict[str, str] = field(default_factory=dict)
    process_env: dict[str, str] = field(default_factory=dict)
    gateway_hits: int = -1
    loopback_hits: int = -1
    primary_hits: int = -1
    host_control: tuple[int, int] = (0, 0)
    bridge_control: str = ""
    bridge_control_hits: int = 0


def _wait_for_role(role: str, deadline: float) -> str:
    while time.monotonic() < deadline:
        ids = docker("ps", "-q", "--no-trunc", "--filter", f"label={ROLE_LABEL}={role}").stdout.split()
        if ids:
            return ids[0]
        time.sleep(0.05)
    pytest.fail(f"no running {role} container appeared")


@pytest.fixture(scope="module")
def live(images: None, tmp_path_factory: pytest.TempPathFactory) -> LiveCheck:
    """One sandbox, held open by a target that doesn't answer until the checks are done. The key
    is in this process's environment for the whole run."""
    assert labelled_ids() == set()
    gateway = json.loads(docker("network", "inspect", "bridge").stdout)[0]["IPAM"]["Config"][0]["Gateway"]
    tmp_path = tmp_path_factory.mktemp("live")
    check = LiveCheck(project=tmp_path / "project", gateway=gateway)
    on_gateway, on_loopback = GatewayListener(gateway), Listener("127.0.0.1")
    on_primary = Listener(primary_host_address())
    aims = json.dumps([
        [gateway, on_gateway.port],
        ["127.0.0.1", on_loopback.port],
        [on_primary.host, on_primary.port],
        ["1.1.1.1", 443],
    ])
    with pytest.MonkeyPatch.context() as patch, ThreadPoolExecutor(1) as pool:
        patch.setenv(KEY_NAME, CANARY)
        future = pool.submit(run_sandboxed, mini_spec(tmp_path, mini_app(GATED_RESERVE)), check.project, "mini", FAST)
        deadline = time.monotonic() + 30
        check.target_id = _wait_for_role("target", deadline)
        check.runner_id = _wait_for_role("runner", deadline)
        try:
            for role, container in (("target", check.target_id), ("runner", check.runner_id)):
                probe = docker("exec", container, "python", "-c", CONNECT_SCRIPT, aims)
                assert probe.returncode == 0, probe.stderr
                check.connects[role] = json.loads(probe.stdout)
                files = docker("exec", container, "sh", "-c", FILE_CHECK_SCRIPT)
                check.files[role] = files.stdout.split("\n")
                check.inspected[role] = docker("inspect", container).stdout
                check.process_env[role] = docker("exec", container, "sh", "-c", "env; cat /proc/1/environ").stdout
            states = docker("inspect", "--format", "{{.State.Running}}", check.target_id, check.runner_id)
            check.both_running = states.stdout.split() == ["true", "true"]
        finally:
            docker("exec", check.target_id, "touch", RELEASE_FILE)
        check.result = future.result(timeout=60)

    check.gateway_hits, check.loopback_hits = on_gateway.hits, on_loopback.hits
    check.primary_hits = on_primary.hits
    # Controls: the listeners do count, and a normally networked container does reach the gateway.
    socket.create_connection(("127.0.0.1", on_loopback.port), timeout=3).close()
    socket.create_connection((on_primary.host, on_primary.port), timeout=3).close()
    time.sleep(0.3)
    check.host_control = (on_loopback.hits - check.loopback_hits, on_primary.hits - check.primary_hits)
    before = on_gateway.hits
    control = docker(
        "run", "--rm", "--pull", "never", "--label", f"{LABEL}=test-control", "--network", "bridge",
        "faultforge-target:dev", "python", "-c",
        f"import socket; socket.create_connection(('{gateway}', {on_gateway.port}), timeout=3).close(); print('connected')",
    )
    time.sleep(0.3)
    check.bridge_control = control.stdout.strip() or control.stderr[-300:]
    check.bridge_control_hits = on_gateway.hits - before
    on_gateway.close()
    on_loopback.close()
    on_primary.close()
    return check


@docker_test
def test_the_live_run_itself_was_a_clean_pass(live: LiveCheck) -> None:
    assert live.both_running, "the checks must run while both containers are up"
    assert live.result.status == "completed", live.result.failure_detail
    assert live.result.report.verdict == "PASS"
    assert live.result.containment.containers_left == 0
    assert labelled_ids() == set()


@docker_test
def test_nothing_can_leave_either_container(live: LiveCheck, capsys: pytest.CaptureFixture) -> None:
    show(capsys, f"egress from inside, gateway {live.gateway}: {json.dumps(live.connects)}")
    assert live.host_control == (1, 1), "the listeners must count a real connection"
    assert live.bridge_control == "connected" and live.bridge_control_hits == 1, live.bridge_control
    assert live.gateway_hits == 0
    assert live.loopback_hits == 0
    assert live.primary_hits == 0
    assert set(live.connects) == {"target", "runner"}
    for role, results in live.connects.items():
        assert len(results) == 5
        for aim, outcome in results.items():
            assert outcome not in ("connected", "resolved"), f"{role} reached {aim}"
        assert results["dns"] == "gaierror"
        routes = int(live.files[role][-2])
        assert routes == 1, f"{role} has routes: only the header line is expected"


@docker_test
def test_runner_shares_only_the_targets_namespace(live: LiveCheck) -> None:
    facts = live.result.containment
    assert facts.target.network_mode == "none"
    assert facts.runner.network_mode == f"container:{facts.target.container_id}"


@docker_test
def test_filesystem_and_privileges_from_inside(live: LiveCheck) -> None:
    target, runner = live.files["target"], live.files["runner"]
    assert target[0] == "65534"
    assert runner[0] == str(os.getuid())
    assert "out-absent" in target, "the target must not see the artifacts"
    assert "out-visible" in runner
    assert "app-readonly" in target
    for lines in (target, runner):
        for expected in ("shm-absent", "socket-absent", "root-readonly", "tmp-writable"):
            assert expected in lines
        assert any(line.split() == ["CapEff:", "0000000000000000"] for line in lines), lines
    mounts = live.result.containment
    assert [(mount.destination, mount.read_only) for mount in mounts.target.mounts] == [("/app", True)]
    assert [(mount.destination, mount.read_only) for mount in mounts.runner.mounts] == [("/out", False)]


@docker_test
def test_the_inference_key_is_absent_everywhere(live: LiveCheck) -> None:
    assert set(live.inspected) == {"target", "runner"}
    for role in live.inspected:
        env = json.loads(live.inspected[role])[0]["Config"]["Env"]
        assert not [item for item in env if CANARY in item or KEY_NAME in item]
        assert CANARY not in live.inspected[role] and KEY_NAME not in live.inspected[role]
        assert CANARY not in live.process_env[role] and KEY_NAME not in live.process_env[role]
    dumped = live.result.model_dump_json()
    assert CANARY not in dumped and KEY_NAME not in dumped
    for path in live.project.rglob("*"):
        if path.is_file():
            assert CANARY.encode() not in path.read_bytes(), path


@docker_test
def test_env_names_are_exactly_the_allowlists(live: LiveCheck) -> None:
    facts = live.result.containment
    assert facts.target.env_names == TARGET_ENV_NAMES
    assert facts.runner.env_names == RUNNER_ENV_NAMES
    assert facts.target.limits_applied and facts.runner.limits_applied
    assert facts.target.memory_bytes == facts.target.memory_swap_bytes == 256 * 2**20
    assert facts.runner.memory_bytes == facts.runner.memory_swap_bytes == 512 * 2**20
    assert facts.target.pids_limit == 128 and facts.runner.pids_limit == 512
    assert facts.target.read_only_rootfs and facts.runner.read_only_rootfs
    assert facts.target.cap_drop == ["ALL"] and facts.runner.cap_drop == ["ALL"]
    assert facts.target.exit_code is None and facts.target.running_when_inspected
    assert facts.runner.exit_code == 0 and facts.runner.oom_killed is False


# 5: hangs


@docker_test
def test_a_target_that_hangs_on_requests_gives_inconclusive(contained: None, tmp_path: Path) -> None:
    options = RunOptions(trials=1, serial_control=False, max_duration_s=2)
    project = tmp_path / "project"
    result = run_sandboxed(mini_spec(tmp_path, mini_app(HANG_RESERVE)), project, "mini", options)
    assert result.status == "completed", result.failure_detail
    assert result.report.verdict == "INCONCLUSIVE"
    assert result.containment.containers_left == 0


@docker_test
def test_a_target_that_never_starts_is_unreachable(contained: None, tmp_path: Path) -> None:
    policy = dataclasses.replace(DEFAULT_POLICY, wait_for_target_s=2)
    project = tmp_path / "project"
    result = run_sandboxed(mini_spec(tmp_path, mini_app(startup="time.sleep(3600)")), project, "mini", FAST, policy)
    assert_failed(result, project, "target_unreachable")
    assert result.containment.containers_left == 0
    assert result.containment.runner.exit_code == 3


@docker_test
def test_the_host_kills_a_run_at_the_wall_limit(contained: None, tmp_path: Path) -> None:
    policy = dataclasses.replace(DEFAULT_POLICY, wall_limit_s=4, wait_for_target_s=30)
    project = tmp_path / "project"
    started = time.monotonic()
    result = run_sandboxed(mini_spec(tmp_path, mini_app(startup="time.sleep(3600)")), project, "mini", FAST, policy)
    assert_failed(result, project, "wall_limit_exceeded")
    assert time.monotonic() - started < 15
    assert result.containment.containers_left == 0
    assert result.containment.runner.running_when_inspected is True


# 6: failure paths


@docker_test
def test_a_target_that_crashes_on_start(contained: None, tmp_path: Path) -> None:
    policy = dataclasses.replace(DEFAULT_POLICY, wait_for_target_s=2)
    project = tmp_path / "project"
    result = run_sandboxed(mini_spec(tmp_path, b"raise SystemExit(7)\n"), project, "mini", FAST, policy)
    assert_failed(result, project, "target_exited", "target_unreachable")
    assert result.containment.containers_left == 0
    assert result.containment.target.running_when_inspected is False
    assert result.containment.target.exit_code != 0


@docker_test
@pytest.mark.parametrize("image_field", ["runner_image", "target_image"])
def test_a_missing_image(contained: None, tmp_path: Path, image_field: str) -> None:
    policy = dataclasses.replace(DEFAULT_POLICY, **{image_field: "faultforge-does-not-exist:dev"})
    project = tmp_path / "project"
    result = run_sandboxed(mini_spec(tmp_path, mini_app()), project, "mini", FAST, policy)
    assert_failed(result, project, "image_missing")
    assert result.containment.containers_left == 0


def test_docker_binary_missing(tmp_path: Path) -> None:
    policy = dataclasses.replace(DEFAULT_POLICY, docker=str(tmp_path / "no-such-docker"))
    project = tmp_path / "project"
    result = run_sandboxed(mini_spec(tmp_path, mini_app()), project, "mini", FAST, policy)
    assert_failed(result, project, "docker_unavailable")
    assert result.containment.containers_left is None


@pytest.mark.parametrize(
    ("script", "timeout_s"),
    [("echo 'Cannot connect to the Docker daemon' >&2; exit 1", 5.0), ("exec sleep 60", 0.3)],
    ids=["daemon-down", "docker-hangs"],
)
def test_docker_broken(tmp_path: Path, script: str, timeout_s: float) -> None:
    fake = tmp_path / "fake-docker"
    fake.write_text(f"#!/bin/sh\n{script}\n", encoding="utf-8")
    fake.chmod(0o755)
    policy = dataclasses.replace(DEFAULT_POLICY, docker=str(fake), docker_timeout_s=timeout_s)
    project = tmp_path / "project"
    result = run_sandboxed(mini_spec(tmp_path, mini_app()), project, "mini", FAST, policy)
    assert_failed(result, project, "docker_unavailable")
    assert result.containment.containers_left is None


@docker_test
def test_a_baseline_from_another_config_is_not_comparable(contained: None, tmp_path: Path) -> None:
    project = tmp_path / "project"
    first = run_sandboxed(mini_spec(tmp_path, mini_app()), project, "mini", FAST)
    assert first.status == "completed", first.failure_detail
    changed = tmp_path / "changed.yaml"
    changed.write_text(MINI_CONFIG.replace("item_id: 1", "item_id: 2"), encoding="utf-8")
    result = run_sandboxed(
        TargetSpec(app_source=mini_app(), config_path=changed), project, "mini", RunOptions(compare_to=first.report.run_id)
    )
    assert result.status == "failed" and result.failure_reason == "not_comparable"
    assert result.report is None


@docker_test
def test_a_runner_killed_mid_run_leaves_a_structured_failure(contained: None, tmp_path: Path) -> None:
    """The whole-runner path of the memory risk: no report, no traceback."""
    project = tmp_path / "project"
    with ThreadPoolExecutor(1) as pool:
        future = pool.submit(run_sandboxed, mini_spec(tmp_path, mini_app(GATED_RESERVE)), project, "mini", FAST)
        runner = _wait_for_role("runner", time.monotonic() + 30)
        assert docker("kill", runner).returncode == 0
        result = future.result(timeout=60)
    assert_failed(result, project, "report_missing")
    assert result.containment.runner.exit_code == 137
    assert result.containment.containers_left == 0


def test_a_silent_runner_with_an_oom_flag_is_reported_as_oom(tmp_path: Path) -> None:
    facts = sandbox._container_facts(
        {"Id": "x", "State": {"OOMKilled": True, "ExitCode": 137}}, DEFAULT_POLICY.runner, "container:", "1:1"
    )
    with pytest.raises(sandbox.SandboxError) as caught:
        sandbox._read_report(tmp_path, "mini", 137, b"", facts)
    assert caught.value.reason == "runner_oom"


@docker_test
@pytest.mark.parametrize(
    ("pids", "reason"), [(4, "runner_error"), (6, None)], ids=["faultforge-cant-start-k6", "k6-dies"]
)
def test_a_runner_out_of_pids_gives_no_pass(contained: None, tmp_path: Path, pids: int, reason: str | None) -> None:
    """Limits this small really run out. At 8, k6 sometimes fits and then a PASS is legitimate."""
    limits = dataclasses.replace(DEFAULT_POLICY.runner, pids=pids)
    project = tmp_path / "project"
    result = run_sandboxed(
        mini_spec(tmp_path, mini_app()), project, "mini", FAST, dataclasses.replace(DEFAULT_POLICY, runner=limits)
    )
    assert result.containment.containers_left == 0
    if reason is not None:
        assert_failed(result, project, reason)
    else:
        assert result.status == "completed", result.failure_detail
        assert result.report.verdict == "INCONCLUSIVE"
        assert [trial.incomplete_reason for trial in result.report.trials] == ["engine_exit_nonzero"]


@docker_test
def test_the_limits_carry_the_largest_allowed_load(contained: None, tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    """The policy mustn't starve a run that's within budget, or it could turn PASS into INCONCLUSIVE."""
    options = RunOptions(trials=1, serial_control=False, vus=LIMITS.max_vus, iterations=LIMITS.max_iterations_per_trial)
    with CgroupWatcher() as watcher:
        result = run_sandboxed(mini_spec(tmp_path, mini_app()), tmp_path / "project", "mini", options)
    show(
        capsys,
        f"{LIMITS.max_vus} VUs, {LIMITS.max_iterations_per_trial} requests: "
        f"verdict={result.report.verdict if result.report else result.failure_reason} "
        f"memory peak {watcher.peak_bytes / 2**20:.0f} MiB, pids peak {watcher.pids_peak}, wall {result.containment.wall_s} s",
    )
    assert result.status == "completed", result.failure_detail
    assert result.report.verdict == "PASS"
    assert watcher.oom_kills == 0


# 7: memory


@docker_test
def test_a_response_too_large_for_k6_is_contained(contained: None, tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    """20 buffered responses of 64 MB can't fit in the runner's limit."""
    config = tmp_path / "big.yaml"
    config.write_text(MINI_CONFIG.replace("vus: 4, iterations: 4", "vus: 20, iterations: 20"), encoding="utf-8")
    spec = TargetSpec(app_source=mini_app(STREAM_RESERVE), config_path=config)
    project = tmp_path / "project"
    with CgroupWatcher() as watcher:
        # Several trials, so the cgroup is still there to sample after the first kill.
        result = run_sandboxed(spec, project, "mini", RunOptions(trials=4, serial_control=False, max_duration_s=10))
    runner = result.containment.runner
    limit = DEFAULT_POLICY.runner.memory_mb * 2**20
    reasons = [trial.incomplete_reason for trial in result.report.trials] if result.report else None
    show(
        capsys,
        f"memory: status={result.status} failure={result.failure_reason} "
        f"verdict={result.report.verdict if result.report else None} trial_reasons={reasons} "
        f"runner_exit={runner.exit_code} runner_oom_flag={runner.oom_killed} "
        f"target_oom_flag={result.containment.target.oom_killed} "
        f"cgroup_oom_kills={watcher.oom_kills} peak={watcher.peak_bytes / 2**20:.0f} MiB "
        f"samples={watcher.samples} wall={result.containment.wall_s} s",
    )
    assert result.containment.containers_left == 0
    assert result.containment.target.oom_killed is False, "the attack must reach k6, not the target"
    assert runner.memory_bytes == limit == runner.memory_swap_bytes
    # The kernel force-charges a dying task's last pages, so memory.peak can pass the limit slightly.
    assert watcher.samples > 0 and watcher.peak_bytes <= limit + MEMORY_OVERSHOOT_BYTES
    assert runner.oom_killed is True, "the test must really exhaust the runner"
    if result.status == "completed":
        assert result.report.verdict == "INCONCLUSIVE"
        assert "engine_exit_nonzero" in reasons
    else:
        assert_failed(result, project, "runner_oom")


# 8: the hostile build


def _hostile_spec(allow: bool) -> TargetSpec:
    assert (HOSTILE / "app.py").is_file(), "demo/hostile-build must exist"
    return TargetSpec(
        app_source=(HOSTILE / "app.py").read_bytes(), config_path=HOSTILE / "faultforge.yaml", allow_hostile=allow
    )


@docker_test
def test_the_hostile_build_is_contained(contained: None, tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    result = run_sandboxed(_hostile_spec(True), tmp_path / "project", "hostile-build")
    facts = result.containment
    finished = {probe["probe"]: probe for probe in facts.probes if probe.get("phase") == "result"}
    table = "; ".join(f"{name}={probe['outcome']} ({probe['detail']})" for name, probe in finished.items())
    show(
        capsys,
        f"hostile: status={result.status} verdict={result.report.verdict if result.report else None} "
        f"wall={facts.wall_s} s target_running={facts.target.running_when_inspected} "
        f"target_oom_flag={facts.target.oom_killed} unfinished={facts.probes_without_result}\n{table}",
    )
    assert result.status == "completed", result.failure_detail
    assert result.report.verdict == "INCONCLUSIVE"
    assert facts.containers_left == 0
    assert facts.target.limits_applied and facts.runner.limits_applied
    assert facts.target.network_mode == "none"
    assert "FF_HOSTILE_ALLOWED" in facts.target.env_names
    assert "FF_HOSTILE_ALLOWED" not in facts.runner.env_names
    assert not [name for name in facts.target.env_names if "KEY" in name or "TOKEN" in name or "SECRET" in name]
    assert facts.probes and all(probe["self_reported"] is True for probe in facts.probes)
    assert len(facts.probes) <= sandbox.MAX_PROBES

    for name in ("egress_external", "egress_dns", "secrets_env", "docker_socket", "write_outside_tmp", "privileges"):
        assert finished[name]["outcome"] == "blocked", finished[name]
    assert finished["memory"]["outcome"] == "blocked", finished["memory"]
    assert finished["fork"]["outcome"] == "blocked", finished["fork"]
    # --cpus throttles a spin but doesn't end it, so this probe mustn't be read as blocked.
    assert finished["cpu_spin"]["outcome"] in ("blocked", "not_blocked")
    assert facts.probes_without_result == []
    # OOMKilled is set when a child dies. It doesn't mean the target did.
    assert facts.target.oom_killed is True
    assert facts.target.running_when_inspected is True and facts.target.exit_code is None


@docker_test
def test_the_hostile_build_refuses_without_permission(contained: None, tmp_path: Path) -> None:
    result = run_sandboxed(_hostile_spec(False), tmp_path / "project", "hostile-build", FAST)
    assert result.status == "completed", result.failure_detail
    assert result.report.verdict == "INCONCLUSIVE"
    assert "FF_HOSTILE_ALLOWED" not in result.containment.target.env_names
    assert {probe["probe"] for probe in result.containment.probes} == {"guard"}


# 9: the reaper


@docker_test
def test_reap_stale_removes_only_labelled_containers(contained: None) -> None:
    command = ("--pull", "never", "--network", "none", "faultforge-target:dev", "sleep", "120")
    labelled = docker("run", "-d", "--label", f"{LABEL}=test-reap", *command).stdout.strip()
    bystander = docker("run", "-d", "--label", "faultforge.test=bystander", *command).stdout.strip()
    try:
        assert labelled and bystander
        assert sandbox.reap_stale(max_age_s=3600) == 0
        assert labelled in labelled_ids()
        before = unlabelled_ids()
        assert sandbox.reap_stale(max_age_s=0) == 1
        assert labelled not in labelled_ids()
        assert unlabelled_ids() == before
        assert bystander in before
    finally:
        # Each by the ID that this test was handed when it created it.
        docker("rm", "-f", labelled)
        docker("rm", "-f", bystander)


def test_reap_stale_reports_docker_being_unavailable(tmp_path: Path) -> None:
    policy = dataclasses.replace(DEFAULT_POLICY, docker=str(tmp_path / "no-such-docker"))
    with pytest.raises(sandbox.SandboxError) as caught:
        sandbox.reap_stale(policy)
    assert caught.value.reason == "docker_unavailable"


# Host-side code identity


def _code_tree(tmp_path: Path, name: str, files: dict[str, bytes]) -> Path:
    root = tmp_path / name
    for relative, content in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    return root


def test_the_code_hash_covers_paths_bytes_and_support_files(tmp_path: Path) -> None:
    base = {"app.py": b"abc", "pkg/mod.py": b"x"}
    hash_of = lambda name, files: sandbox.code_dir_sha256(_code_tree(tmp_path, name, files))
    first = hash_of("a", base)
    assert first == hash_of("same", dict(reversed(base.items()))), "order of creation must not matter"
    assert first != hash_of("app-edited", {**base, "app.py": b"abd"})
    assert first != hash_of("support-edited", {**base, "pkg/mod.py": b"y"})
    assert first != hash_of("renamed", {"app.py": b"abc", "pkg/other.py": b"x"})
    assert first != hash_of("added", {**base, "pkg/new.py": b""})
    assert hash_of("boundary-1", {"a": b"bc"}) != hash_of("boundary-2", {"ab": b"c"})


def test_the_hash_is_of_what_was_written_for_the_target(tmp_path: Path) -> None:
    support = _code_tree(tmp_path, "support_pkg", {"mod.py": b"x = 1\n"})
    spec = TargetSpec(app_source=b"app", config_path=tmp_path, support_dir=support)
    code = sandbox.write_code_dir(spec)
    try:
        expected = sandbox.code_dir_sha256(_code_tree(tmp_path, "copy", {"app.py": b"app", "support_pkg/mod.py": b"x = 1\n"}))
        assert sandbox.code_dir_sha256(code) == expected
    finally:
        shutil.rmtree(code)


# The state verifier, without containers


def _load_demo_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("FF_DB_PATH", str(tmp_path / "ticket.db"))
    spec_ = importlib.util.spec_from_file_location("ff_demo_db_for_sync", SUPPORT / "db.py")
    module = importlib.util.module_from_spec(spec_)
    spec_.loader.exec_module(module)
    return module


def _sql_in_db_state() -> dict[str, str]:
    """Maps each field of the dict that db.state() returns to the SQL that computes it."""
    tree = ast.parse((SUPPORT / "db.py").read_text(encoding="utf-8"))
    function = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "state")
    statements = {
        node.targets[0].id: next(
            child.value for child in ast.walk(node.value)
            if isinstance(child, ast.Constant) and isinstance(child.value, str)
        )
        for node in function.body
        if isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name) and node.targets[0].id != "conn"
    }
    returned = next(node.value for node in function.body if isinstance(node, ast.Return))
    return {key.value: statements[value.id] for key, value in zip(returned.keys, returned.values, strict=True)}


def test_the_verifier_has_the_same_sql_as_db_state() -> None:
    normalized = {name: " ".join(sql.split()) for name, sql in _sql_in_db_state().items()}
    assert normalized == dict(TICKET_API_STATE_VERIFIER)


def test_the_verifier_agrees_with_db_state_on_real_rows(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db = _load_demo_db(tmp_path, monkeypatch)
    db.init(initial_stock=5)
    conn = db.connection()
    conn.execute("INSERT INTO payments VALUES ('p-attached', 'a', 'authorized')")
    conn.execute("INSERT INTO payments VALUES ('p-orphan', 'b', 'authorized')")
    conn.execute("INSERT INTO payments VALUES ('p-pending', 'c', 'void_pending')")
    conn.execute("INSERT INTO payments VALUES ('p-voided', 'd', 'voided')")
    db.insert_order(1, "a", "p-attached")
    db.insert_order(1, "e", None)
    db.insert_order(1, "f", "p-voided")
    expected = db.state()
    assert expected == {"stock": 5, "orders_confirmed": 3, "payments_unattached": 2, "orders_without_payment": 2}
    assert _run_verifier_script(tmp_path / "ticket.db", TICKET_API_STATE_VERIFIER) == expected


def _run_verifier_script(path: Path, queries) -> dict[str, int]:
    """The real script and flags, on this machine's Python."""
    argv = sandbox.state_verifier_argv("target", queries)
    assert argv[:4] == ["exec", "--workdir", "/", "target"]
    command = [sys.executable, *argv[5:]]
    command[command.index(f"file:{sandbox.TARGET_DB_PATH}?mode=ro")] = f"file:{path}?mode=ro"
    result = subprocess.run(command, capture_output=True, timeout=30, check=True)
    return sandbox.parse_state_output(result.stdout, queries)


def test_the_verifier_runs_isolated_and_read_only(tmp_path: Path) -> None:
    argv = sandbox.state_verifier_argv("target", TICKET_API_STATE_VERIFIER)
    assert argv[4:7] == ["python", "-I", "-c"] and argv[7] == sandbox.STATE_VERIFIER_SCRIPT
    assert f"file:{sandbox.TARGET_DB_PATH}?mode=ro" in argv
    path = tmp_path / "ticket.db"
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE items (stock INTEGER)")
    connection.execute("INSERT INTO items VALUES (7)")
    connection.commit()
    connection.close()
    assert _run_verifier_script(path, {"stock": "SELECT SUM(stock) FROM items"}) == {"stock": 7}
    with pytest.raises(subprocess.CalledProcessError) as caught:
        _run_verifier_script(path, {"stock": "UPDATE items SET stock = 0"})
    assert b"readonly" in caught.value.stderr
    assert sqlite3.connect(path).execute("SELECT stock FROM items").fetchone() == (7,)


def test_the_target_gets_the_database_path_the_verifier_reads() -> None:
    assert sandbox.target_env(False)["FF_DB_PATH"] == sandbox.TARGET_DB_PATH


def test_the_verifier_mapping_is_frozen(tmp_path: Path) -> None:
    with pytest.raises(TypeError):
        TICKET_API_STATE_VERIFIER["stock"] = "SELECT 0"  # type: ignore[index]
    original = {"stock": "SELECT 1"}
    spec = TargetSpec(app_source=b"x", config_path=tmp_path, state_verifier=original)
    original["stock"] = "SELECT 2"
    assert spec.state_verifier["stock"] == "SELECT 1"
    with pytest.raises(TypeError):
        spec.state_verifier["stock"] = "SELECT 3"  # type: ignore[index]


@pytest.mark.parametrize(
    "output",
    [
        b"",
        b"not json",
        b"[1]",
        b"1",
        b'{"a": 1}',
        b'{"a": 1, "b": 2, "c": 3}',
        b'{"a": 1, "b": "2"}',
        b'{"a": 1, "b": 2.0}',
        b'{"a": 1, "b": true}',
        b'{"a": 1, "b": null}',
        b'{"a": 1, "b": NaN}',
        b'{"a": 1, "b": 9223372036854775808}',
        b'{"a": 1, "b": ' + b"9" * 5000 + b"}",
        b'{"a": 1, "b": 2}' + b" " * sandbox.MAX_STATE_OUTPUT_BYTES,
        b"[" * 100000,
    ],
)
def test_verifier_output_is_validated_as_untrusted(output: bytes) -> None:
    with pytest.raises(ValueError):
        sandbox.parse_state_output(output, {"a": "SELECT 1", "b": "SELECT 2"})


def test_good_verifier_output_is_accepted() -> None:
    assert sandbox.parse_state_output(b'{"a": 0, "b": -2}\n', {"a": "x", "b": "y"}) == {"a": 0, "b": -2}


RUN_ID = "run_20260101_000000_abcd"


def _reported(tmp_path: Path, trials: dict[int, bytes]) -> SimpleNamespace:
    """A project with those trials' state-after.json files. Skipped trials have no bytes."""
    directory = sandbox.report_module.run_dir(tmp_path, RUN_ID)
    for number, content in trials.items():
        (directory / "trials" / str(number)).mkdir(parents=True)
        if content:
            (directory / "trials" / str(number) / "state-after.json").write_bytes(content)
    return SimpleNamespace(run_id=RUN_ID, trials=[SimpleNamespace(trial=number) for number in trials])


def test_matching_state_is_matched(tmp_path: Path) -> None:
    report = _reported(tmp_path, {1: b'{"stock": 0, "orders": 1, "in_flight": 0, "build": {}}'})
    facts = sandbox.compare_state({"stock": 0, "orders": 1}, tmp_path, report)
    assert facts.status == "matched" and facts.mismatches == [] and facts.reason is None


def test_a_mismatch_names_the_field_and_both_values(tmp_path: Path) -> None:
    report = _reported(tmp_path, {1: b'{"stock": 0, "orders": 1}'})
    facts = sandbox.compare_state({"stock": 0, "orders": 19}, tmp_path, report)
    assert facts.status == "mismatch"
    assert [item.model_dump() for item in facts.mismatches] == [{"name": "orders", "reported": 1, "database": 19}]


@pytest.mark.parametrize("value", [b'"0"', b"0.0", b"false", b"null", b"[0]", b"9223372036854775808"])
def test_a_reported_value_that_is_not_an_int_is_a_mismatch(tmp_path: Path, value: bytes) -> None:
    report = _reported(tmp_path, {1: b'{"stock": ' + value + b"}"})
    facts = sandbox.compare_state({"stock": 0}, tmp_path, report)
    assert facts.status == "mismatch" and facts.mismatches[0].reported is None


def test_a_missing_reported_field_is_a_mismatch(tmp_path: Path) -> None:
    facts = sandbox.compare_state({"stock": 0}, tmp_path, _reported(tmp_path, {1: b"{}"}))
    assert facts.status == "mismatch" and facts.mismatches[0].reported is None


def test_only_the_final_trial_is_compared(tmp_path: Path) -> None:
    report = _reported(tmp_path, {1: b'{"stock": 99}', 2: b'{"stock": 0}'})
    assert sandbox.compare_state({"stock": 0}, tmp_path, report).status == "matched"


def test_an_earlier_trial_never_stands_in_for_the_final_one(tmp_path: Path) -> None:
    """A skipped final trial has already reset the database, so the earlier numbers can't agree."""
    report = _reported(tmp_path, {1: b'{"stock": 0}', 2: b""})
    facts = sandbox.compare_state({"stock": 0}, tmp_path, report)
    assert facts.status == "unavailable" and facts.reason == "state_after_missing"


@pytest.mark.parametrize(
    "content", [b"not json", b"[1]", b'{"stock": ' + b"9" * 5000 + b"}", b" " * (sandbox.MAX_STATE_AFTER_BYTES + 1)]
)
def test_an_unusable_state_file_is_unavailable(tmp_path: Path, content: bytes) -> None:
    facts = sandbox.compare_state({"stock": 0}, tmp_path, _reported(tmp_path, {1: content}))
    assert facts.status == "unavailable"


def test_no_trials_is_unavailable(tmp_path: Path) -> None:
    report = SimpleNamespace(run_id=RUN_ID, trials=[])
    assert sandbox.compare_state({"stock": 0}, tmp_path, report).status == "unavailable"


@pytest.mark.parametrize(
    "verifier",
    [{}, {"Bad-Name": "SELECT 1"}, {"stock": ""}, {"stock": 1}],
    ids=["empty", "bad-name", "no-sql", "not-text"],
)
def test_a_bad_verifier_never_starts_a_container(tmp_path: Path, verifier: dict) -> None:
    policy = dataclasses.replace(DEFAULT_POLICY, docker=str(tmp_path / "docker-must-not-run"))
    spec = dataclasses.replace(ticket_spec(BUGGY), state_verifier=verifier)
    result = run_sandboxed(spec, tmp_path / "project", SCENARIO, FAST, policy)
    assert result.status == "failed" and result.failure_reason == "invalid_spec"
    assert result.containment.state_verification.status == "unavailable"


def test_no_verifier_means_no_verification(tmp_path: Path) -> None:
    policy = dataclasses.replace(DEFAULT_POLICY, docker=str(tmp_path / "no-such-docker"))
    result = run_sandboxed(ticket_spec(BUGGY), tmp_path / "project", SCENARIO, FAST, policy)
    assert result.containment.state_verification is None


def test_a_run_with_no_report_has_no_verified_state(tmp_path: Path) -> None:
    policy = dataclasses.replace(DEFAULT_POLICY, docker=str(tmp_path / "no-such-docker"))
    spec = dataclasses.replace(ticket_spec(BUGGY), state_verifier=TICKET_API_STATE_VERIFIER)
    facts = run_sandboxed(spec, tmp_path / "project", SCENARIO, FAST, policy).containment.state_verification
    assert facts.status == "unavailable" and facts.reason == "no_report"


def _fake_docker(tmp_path: Path, body: str) -> Path:
    fake = tmp_path / "fake-docker"
    fake.write_text(f"#!/bin/sh\n{body}\n", encoding="utf-8")
    fake.chmod(0o755)
    return fake


def _session_reading_database(tmp_path: Path, body: str, timeout_s: float = 5.0) -> sandbox._Session:
    policy = dataclasses.replace(DEFAULT_POLICY, docker=str(_fake_docker(tmp_path, body)), docker_timeout_s=timeout_s)
    spec = dataclasses.replace(ticket_spec(BUGGY), state_verifier={"stock": "SELECT 1"})
    return sandbox._Session(spec, tmp_path, SCENARIO, FAST, policy, time.monotonic())


RUNNING = 'if [ "$1" = inspect ]; then echo true; exit 0; fi\n'


@pytest.mark.parametrize(
    ("body", "timeout_s", "reason"),
    [
        ('if [ "$1" = inspect ]; then echo false; exit 0; fi\necho \'{"stock": 1}\'', 5.0, "target_exited"),
        (RUNNING + "echo 'private text' >&2; exit 1", 5.0, "exec_exited_1"),
        (RUNNING + "echo garbage", 5.0, "invalid_output"),
        (RUNNING + "echo '{\"stock\": true}'", 5.0, "invalid_output"),
        (RUNNING + "head -c 200000 /dev/zero", 5.0, "invalid_output"),
        (RUNNING + "exec sleep 60", 0.3, "exec_timed_out"),
        ("exit 0", 5.0, "target_exited"),
    ],
    ids=["exited", "exec-fails", "garbage", "wrong-type", "oversized", "hangs", "not-found"],
)
def test_a_database_read_that_fails_is_unavailable_and_never_raises(
    tmp_path: Path, body: str, timeout_s: float, reason: str
) -> None:
    session = _session_reading_database(tmp_path, body, timeout_s)
    session._read_database()
    report = _reported(tmp_path, {1: b'{"stock": 1}'})
    facts = session.state_verification(report)
    assert facts.status == "unavailable" and facts.reason == reason
    assert "private" not in facts.model_dump_json()


def test_a_database_read_that_works_is_compared(tmp_path: Path) -> None:
    session = _session_reading_database(tmp_path, RUNNING + "echo '{\"stock\": 1}'")
    session._read_database()
    assert session.state_verification(_reported(tmp_path, {1: b'{"stock": 1}'})).status == "matched"


# The reaper, without containers


def test_every_container_is_labelled_with_the_sessions_expiry(tmp_path: Path) -> None:
    """Fake docker: records every call, and has no containers and no answer to wait."""
    log = tmp_path / "calls"
    fake = _fake_docker(tmp_path, f'printf "%s\\n" "$*" >> {log}\nexit 0')
    policy = dataclasses.replace(DEFAULT_POLICY, docker=str(fake), wall_limit_s=50, expiry_margin_s=70)
    before = time.time()
    run_sandboxed(mini_spec(tmp_path, mini_app()), tmp_path / "project", "mini", FAST, policy)
    runs = [line for line in log.read_text().splitlines() if line.startswith("run ")]
    assert len(runs) == 2
    for line in runs:
        words = line.split()
        label = next(word for word in words if word.startswith(f"{EXPIRES_LABEL}="))
        expires = int(label.partition("=")[2])
        assert before + 120 <= expires <= time.time() + 122


def test_the_expiry_margin_covers_collecting_and_removing() -> None:
    assert DEFAULT_POLICY.expiry_margin_s >= 13 * DEFAULT_POLICY.docker_timeout_s


NOW = datetime.now(UTC)


@pytest.mark.parametrize(
    ("labels", "age_s", "max_age_s", "stale"),
    [
        ({EXPIRES_LABEL: str(int(NOW.timestamp()) + 100)}, 10_000, 300, False),
        ({EXPIRES_LABEL: str(int(NOW.timestamp()) - 1)}, 1, 300, True),
        ({}, 10_000, 300, True),
        ({}, 1, 300, False),
        ({EXPIRES_LABEL: "soon"}, 10_000, 300, True),
        ({EXPIRES_LABEL: "soon"}, 1, 300, False),
        ({EXPIRES_LABEL: ""}, 10_000, 300, True),
        ({EXPIRES_LABEL: "-1"}, 1, 300, False),
        ({EXPIRES_LABEL: "1e12"}, 1, 300, False),
        ({EXPIRES_LABEL: "١٢٣"}, 1, 300, False),
        ({EXPIRES_LABEL: "9" * 40}, 10_000, 300, True),
    ],
    ids=[
        "live-but-old", "expired-but-new", "unlabelled-old", "unlabelled-new", "malformed-old", "malformed-new",
        "empty-old", "negative-new", "exponent-new", "non-ascii-digits-new", "absurd-old",
    ],
)
def test_expiry_decides_and_anything_else_falls_back_to_age(
    labels: dict, age_s: float, max_age_s: float, stale: bool
) -> None:
    created = NOW - timedelta(seconds=age_s)
    assert sandbox._is_stale(labels, created, NOW, max_age_s) is stale


# Tests that can run next to another process's sandboxes. They assert nothing about labelled
# containers in general, and remove only what they created, found by its exact label.


@pytest.fixture
def usable_images(request: pytest.FixtureRequest) -> None:
    """FF_TEST_USE_BUILT_IMAGES skips the build, because a retag would change what another running
    process gets next. These tests' subject runs on the host, so the runner image's age doesn't matter."""
    if not os.environ.get("FF_TEST_USE_BUILT_IMAGES"):
        request.getfixturevalue("images")
        return
    for image in (DEFAULT_POLICY.target_image, DEFAULT_POLICY.runner_image):
        assert docker("image", "inspect", "--format", "{{.Id}}", image).returncode == 0, image


# The reaper inside each run can't reach any other process's container, whatever its age.
ALONGSIDE = dataclasses.replace(DEFAULT_POLICY, stale_after_s=7 * 24 * 3600)
FORGED_STATE = (TICKET_API / "false_fixes" / "sloppy_no_void" / "app.py").read_bytes() + b"""
_real_state = db.state
db.state = lambda: {**_real_state(), "payments_unattached": 0}
"""


def verified_spec(app: bytes) -> TargetSpec:
    return TargetSpec(
        app_source=app, support_dir=SUPPORT, config_path=TICKET_CONFIG, state_verifier=TICKET_API_STATE_VERIFIER
    )


@docker_test
def test_forged_state_is_a_mismatch_and_the_honest_fix_matches(
    usable_images: None, tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    project = tmp_path / "project"
    buggy = run_sandboxed(verified_spec(BUGGY), project, SCENARIO, RunOptions(), ALONGSIDE)
    assert buggy.status == "completed", buggy.failure_detail
    assert buggy.report.verdict == "FAIL"
    compare = RunOptions(compare_to=buggy.report.run_id)

    forged = run_sandboxed(verified_spec(FORGED_STATE), project, SCENARIO, compare, ALONGSIDE)
    honest = run_sandboxed(verified_spec(REFERENCE_FIX), project, SCENARIO, compare, ALONGSIDE)
    assert forged.status == "completed", forged.failure_detail
    assert honest.status == "completed", honest.failure_detail
    facts = forged.containment.state_verification
    show(
        capsys,
        f"forged: verdict={forged.report.verdict} fix_verified={forged.report.comparison.fix_verified} "
        f"state_verification={facts.model_dump_json()}\n"
        f"honest: verdict={honest.report.verdict} fix_verified={honest.report.comparison.fix_verified} "
        f"state_verification={honest.containment.state_verification.model_dump_json()}",
    )
    # Without the direct check this forged build would have read as a verified fix.
    assert forged.report.verdict == "PASS" and forged.report.comparison.fix_verified is True
    assert facts.status == "mismatch"
    assert [item.name for item in facts.mismatches] == ["payments_unattached"]
    assert facts.mismatches[0].reported == 0 and facts.mismatches[0].database > 0

    assert honest.report.comparison.fix_verified is True
    assert honest.containment.state_verification.status == "matched"

    hashes = {result.deployed_code_sha256 for result in (buggy, forged, honest)}
    assert len(hashes) == 3 and None not in hashes
    assert buggy.containment.state_verification.status == "matched"
    for result in (buggy, forged, honest):
        assert result.containment.containers_left == 0
        assert "state_verification" not in result.report.model_dump_json()


EVIL_SQLITE3 = """
class _Cursor:
    def fetchone(self):
        return (1000,)

class _Connection:
    def execute(self, *_):
        return _Cursor()

def connect(*_, **__):
    return _Connection()
"""


@docker_test
def test_app_code_on_pythonpath_cannot_answer_for_the_database(usable_images: None, tmp_path: Path) -> None:
    """/app/sqlite3 is on the target's PYTHONPATH and would answer 1000, the value this app reports.
    -I means the real sqlite3 runs instead, and this app has no database to open."""
    evil = tmp_path / "sqlite3"
    evil.mkdir()
    (evil / "__init__.py").write_text(EVIL_SQLITE3, encoding="utf-8")
    spec = dataclasses.replace(
        mini_spec(tmp_path, mini_app()), support_dir=evil, state_verifier={"stock": "SELECT SUM(stock) FROM items"}
    )
    result = run_sandboxed(spec, tmp_path / "project", "mini", FAST, ALONGSIDE)
    assert result.status == "completed", result.failure_detail
    facts = result.containment.state_verification
    assert facts.status == "unavailable" and facts.reason.startswith("exec_exited_")
    assert result.containment.containers_left == 0


@pytest.fixture
def own_containers(usable_images: None, tmp_path: Path) -> Iterator[Callable[[str | None], str]]:
    """Each container has its own sandbox id, and is removed by that exact label."""
    made: list[str] = []
    real_docker = shutil.which("docker")
    assert real_docker

    def start(expires: str | None) -> str:
        sandbox_id = f"test-reap-{secrets.token_hex(6)}"
        labels = ["--label", f"{LABEL}={sandbox_id}"]
        if expires is not None:
            labels += ["--label", f"{EXPIRES_LABEL}={expires}"]
        started = docker(
            "run", "-d", "--pull", "never", "--network", "none", *labels, DEFAULT_POLICY.target_image, "sleep", "120"
        )
        assert started.returncode == 0, started.stderr
        made.append(sandbox_id)
        return sandbox_id

    yield start
    for sandbox_id in made:
        for container in docker("ps", "-aq", "--no-trunc", "--filter", f"label={LABEL}={sandbox_id}").stdout.split():
            docker("rm", "-f", container)


def _exists(sandbox_id: str) -> bool:
    return bool(docker("ps", "-aq", "--filter", f"label={LABEL}={sandbox_id}").stdout.strip())


def _policy_seeing_only(tmp_path: Path, sandbox_ids: list[str]) -> sandbox.SandboxPolicy:
    """The real reaper and real docker, except that its list holds only this test's containers, so
    it can't pick another process's, even with an age limit of zero."""
    ids = []
    for sandbox_id in sandbox_ids:
        ids += docker("ps", "-aq", "--no-trunc", "--filter", f"label={LABEL}={sandbox_id}").stdout.split()
    wrapper = _fake_docker(tmp_path, f'if [ "$1" = ps ]; then echo {" ".join(ids)}; exit 0; fi\nexec {shutil.which("docker")} "$@"')
    return dataclasses.replace(DEFAULT_POLICY, docker=str(wrapper))


@docker_test
def test_reaper_keeps_a_live_container_whatever_its_age(own_containers, tmp_path: Path) -> None:
    live = own_containers(str(int(time.time()) + 3600))
    assert sandbox.reap_stale(_policy_seeing_only(tmp_path, [live]), max_age_s=0) == 0
    assert _exists(live)


@docker_test
def test_reaper_removes_an_expired_container_whatever_its_age(own_containers, tmp_path: Path) -> None:
    expired = own_containers(str(int(time.time()) - 1))
    assert sandbox.reap_stale(_policy_seeing_only(tmp_path, [expired]), max_age_s=3600) == 1
    assert not _exists(expired)


@docker_test
@pytest.mark.parametrize("expires", [None, "soon"], ids=["no-label", "malformed"])
def test_reaper_falls_back_to_age(own_containers, tmp_path: Path, expires: str | None) -> None:
    older_style = own_containers(expires)
    policy = _policy_seeing_only(tmp_path, [older_style])
    assert sandbox.reap_stale(policy, max_age_s=3600) == 0
    assert _exists(older_style)
    assert sandbox.reap_stale(policy, max_age_s=0) == 1
    assert not _exists(older_style)


def test_hostile_probe_names_cannot_crash_the_manager():
    probes = sandbox.parse_probes(
        b'FF_PROBE {"probe": ["a"], "phase": "started"}\n'
        b'FF_PROBE {"probe": {"b": 1}, "phase": "result"}\n'
        b'FF_PROBE {"probe": "real", "phase": "started"}\n'
    )
    assert sandbox.unfinished_probes(probes) == ["real"]
