"""M1 acceptance tests. These run real uvicorn servers and send real concurrent requests."""

import asyncio
import contextlib
import os
import shutil
import signal
import socket
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest

TICKET_API = Path(__file__).resolve().parents[1] / "demo" / "ticket-api"
sys.path.insert(0, str(TICKET_API))

from race_check import run_attempt  # noqa: E402

BUGGY = TICKET_API
REFERENCE_FIX = TICKET_API / "reference_fix"
SLOPPY_FIX = TICKET_API / "false_fixes" / "sloppy_no_void"
REJECT_ALL = TICKET_API / "false_fixes" / "reject_all"
NO_PAYMENT = TICKET_API / "false_fixes" / "no_payment"
ATTEMPTS = 10
REQUESTS = 20


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class Server:
    def __init__(self, app_dir: Path, db_path: Path, support_dir: Path = TICKET_API, workers: int = 1):
        self.app_dir, self.db_path, self.support_dir = app_dir, db_path, support_dir
        self.workers = workers
        self.port = free_port()
        self.url = f"http://127.0.0.1:{self.port}"
        self.process: subprocess.Popen | None = None

    def start(self) -> "Server":
        env = {
            "PATH": os.environ["PATH"],
            "PYTHONPATH": str(self.support_dir),
            "FF_DB_PATH": str(self.db_path),
            "FF_TEST_HOOKS": "1",
            # A same-size edit within one second would otherwise run stale bytecode.
            "PYTHONDONTWRITEBYTECODE": "1",
        }
        self.log_path = self.db_path.with_suffix(".log")
        with self.log_path.open("ab") as log:
            self.process = subprocess.Popen(
                [
                    sys.executable, "-m", "uvicorn", "app:app",
                    "--app-dir", str(self.app_dir),
                    "--port", str(self.port),
                    "--log-level", "warning",
                    "--workers", str(self.workers),
                ],
                env=env,
                stdout=log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            with contextlib.suppress(httpx.TransportError):
                if httpx.get(f"{self.url}/__ff/state", timeout=1).status_code == 200:
                    return self
            time.sleep(0.05)
        self.stop()
        raise RuntimeError(f"server for {self.app_dir} didn't start")

    def stop(self) -> None:
        if self.process is None:
            return
        with contextlib.suppress(ProcessLookupError):
            os.killpg(self.process.pid, signal.SIGTERM)
        try:
            self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            os.killpg(self.process.pid, signal.SIGKILL)
            self.process.wait()
        self.process = None

    def log_tail(self) -> str:
        return self.log_path.read_text(errors="replace")[-2000:]

    def reset(self, stock: int) -> None:
        httpx.post(f"{self.url}/__ff/reset", json={"stock": stock}, timeout=5).raise_for_status()

    def reserve(self, buyer: str, item_id: int = 1) -> httpx.Response:
        return httpx.post(
            f"{self.url}/reserve", json={"item_id": item_id, "buyer": buyer}, timeout=5
        )

    def state(self) -> dict:
        return httpx.get(f"{self.url}/__ff/state", timeout=5).json()


@pytest.fixture
def serve(tmp_path) -> Iterator:
    servers = []

    def start(app_dir: Path, **kwargs) -> Server:
        server = Server(app_dir, tmp_path / f"{len(servers)}.db", **kwargs).start()
        servers.append(server)
        return server

    yield start
    for server in servers:
        server.stop()


def race(server: Server) -> dict:
    return asyncio.run(run_attempt(server.url, REQUESTS))


# The bug: real, reproducible, and invisible to sequential tests


def test_sequential_behaviour_looks_correct_on_buggy_code(serve):
    server = serve(BUGGY)
    server.reset(1)
    assert server.reserve("alice").status_code == 200
    sold_out = server.reserve("bob")
    assert sold_out.status_code == 409
    assert server.reserve("carol", item_id=99).status_code == 404
    state = server.state()
    assert state["stock"] == 0
    assert state["orders_confirmed"] == 1
    assert state["payments_unattached"] == 0
    assert state["orders_without_payment"] == 0


def test_buggy_code_oversells_every_attempt(serve):
    server = serve(BUGGY)
    for _ in range(ATTEMPTS):
        result = race(server)
        state = result["state"]
        assert result["statuses"].get("200", 0) > 1, result
        # Stock looks healthy while more than one order exists.
        assert state["stock"] == 0
        assert state["orders_confirmed"] == result["statuses"]["200"]
        # The accounting is wrong, but nothing else is flagged.
        assert state["payments_unattached"] == 0
        assert state["orders_without_payment"] == 0


# The reference fix


def assert_exactly_one_sale(result: dict) -> None:
    assert result["statuses"] == {"200": 1, "409": REQUESTS - 1}, result
    state = result["state"]
    assert state["stock"] == 0
    assert state["orders_confirmed"] == 1
    assert state["payments_unattached"] == 0
    assert state["orders_without_payment"] == 0


def test_reference_fix_holds_under_concurrency(serve):
    server = serve(REFERENCE_FIX)
    for _ in range(ATTEMPTS):
        assert_exactly_one_sale(race(server))


def test_reference_fix_holds_across_worker_processes(serve):
    # A connection reset was seen once across 4 workers and couldn't be reproduced (docs/VERIFIED.md).
    # It isn't a sale outcome and each attempt re-seeds state, so one retry is allowed. Every
    # attempt that completes is still checked, and a second reset fails with the server's log.
    server = serve(REFERENCE_FIX, workers=4)
    resets = 0
    completed = 0
    while completed < ATTEMPTS:
        try:
            result = race(server)
        except httpx.TransportError as error:
            resets += 1
            assert resets <= 1, f"repeated connection resets: {error!r}\n{server.log_tail()}"
            continue
        assert_exactly_one_sale(result)
        completed += 1


# Failure paths on the reference fix


def test_decline_changes_nothing(serve):
    server = serve(REFERENCE_FIX)
    server.reset(1)
    assert server.reserve("decline-alice").status_code == 402
    state = server.state()
    assert (state["stock"], state["orders_confirmed"], state["payments_unattached"]) == (1, 0, 0)


def test_authorize_timeout_voids_the_hold(serve):
    server = serve(REFERENCE_FIX)
    server.reset(1)
    assert server.reserve("timeout-alice").status_code == 503
    state = server.state()
    assert (state["stock"], state["orders_confirmed"], state["payments_unattached"]) == (1, 0, 0)


def test_exception_in_transaction_rolls_back_and_voids(serve):
    server = serve(REFERENCE_FIX)
    server.reset(1)
    assert server.reserve("crash-alice").status_code == 500
    state = server.state()
    assert (state["stock"], state["orders_confirmed"], state["payments_unattached"]) == (1, 0, 0)


def test_losing_the_claim_voids_the_hold(serve):
    server = serve(REFERENCE_FIX)
    server.reset(0)
    assert server.reserve("alice").status_code == 409
    assert server.state()["payments_unattached"] == 0


def test_failed_void_stays_visible(serve):
    server = serve(REFERENCE_FIX)
    server.reset(0)
    assert server.reserve("voidfail-alice").status_code == 409
    assert server.state()["payments_unattached"] == 1


# False fixes must be detectable from state and responses


def test_sloppy_fix_leaves_orphaned_holds(serve):
    result = race(serve(SLOPPY_FIX))
    assert result["statuses"] == {"200": 1, "409": REQUESTS - 1}
    assert result["state"]["payments_unattached"] == REQUESTS - 1


def test_reject_all_fix_sells_nothing(serve):
    result = race(serve(REJECT_ALL))
    assert result["statuses"] == {"409": REQUESTS}
    assert result["state"]["stock"] == 1
    assert result["state"]["orders_confirmed"] == 0


def test_removing_payment_leaves_an_unpaid_order(serve):
    result = race(serve(NO_PAYMENT))
    assert result["statuses"] == {"200": 1, "409": REQUESTS - 1}
    assert result["state"]["orders_without_payment"] == 1


# Fingerprint and hook gating


@pytest.fixture
def app_copy(tmp_path) -> Path:
    target = tmp_path / "copy"
    target.mkdir()
    shutil.copy(BUGGY / "app.py", target / "app.py")
    shutil.copytree(BUGGY / "ticket_support", target / "ticket_support")
    return target


def build_of(server: Server) -> dict:
    return server.state()["build"]


def test_restart_changes_only_instance_id(serve, app_copy):
    first = serve(app_copy, support_dir=app_copy)
    before = build_of(first)
    first.stop()
    after = build_of(serve(app_copy, support_dir=app_copy))
    assert after["build_id"] == before["build_id"]
    assert after["instance_id"] != before["instance_id"]


def test_editing_code_and_restarting_changes_build_id(serve, app_copy):
    first = serve(app_copy, support_dir=app_copy)
    before = build_of(first)
    first.stop()
    source = app_copy / "app.py"
    source.write_text(source.read_text().replace("stock - 1", "stock - 0"))
    after = build_of(serve(app_copy, support_dir=app_copy))
    assert after["build_id"] != before["build_id"]
    assert after["instance_id"] != before["instance_id"]


def test_editing_without_restart_is_stale_and_keeps_build_id(serve, app_copy):
    server = serve(app_copy, support_dir=app_copy)
    before = build_of(server)
    assert server.state()["stale_source"] is False
    source = app_copy / "app.py"
    source.write_text(source.read_text() + "\n# edited\n")
    os.utime(source, (time.time() + 5, time.time() + 5))
    state = server.state()
    assert state["stale_source"] is True
    assert state["build"]["build_id"] == before["build_id"]


def test_hooks_are_off_by_default(tmp_path):
    port = free_port()
    env = {
        "PATH": os.environ["PATH"],
        "PYTHONPATH": str(TICKET_API),
        "FF_DB_PATH": str(tmp_path / "off.db"),
    }
    process = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app:app", "--app-dir", str(BUGGY),
         "--port", str(port), "--log-level", "critical"],
        env=env,
    )
    try:
        url = f"http://127.0.0.1:{port}"
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            with contextlib.suppress(httpx.TransportError):
                if httpx.get(f"{url}/health", timeout=1).status_code == 200:
                    break
            time.sleep(0.05)
        assert httpx.get(f"{url}/__ff/state", timeout=2).status_code == 404
        assert httpx.post(f"{url}/__ff/reset", json={"stock": 5}, timeout=2).status_code == 404
    finally:
        process.terminate()
        process.wait(timeout=5)
