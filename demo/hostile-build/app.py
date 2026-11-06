"""INTENTIONALLY HOSTILE TEST FIXTURE. See README.md. Run it only inside a limited container.

The containment target for FaultForge's own sandbox (docs/PLAN.md V3.6). POST /reserve tries to
misbehave and prints what happened, so the demo can show each attempt being blocked. It never
sends data anywhere: egress attempts are bare connects, and secret scans report names only.

Nothing runs unless guard_refusal() allows it. On a developer's machine this would be a real
memory and fork bomb, so the guard also runs at import and again before every child process.
"""

import asyncio
import errno
import hashlib
import json
import os
import re
import signal
import socket
import subprocess
import sys
import threading
import time
import uuid
from collections.abc import Callable, Iterable
from datetime import UTC, datetime
from pathlib import Path

from fastapi import FastAPI, Request, Response
from pydantic import BaseModel, Field

MAX_DETAIL_CHARS = 160
ROUTE_TABLE = "/proc/net/route"
DOCKERENV = "/.dockerenv"
ALLOW_ENV = "FF_HOSTILE_ALLOWED"
EGRESS_TIMEOUT_S = 2.0
DISCONNECT_POLL_S = 0.25

MEMORY_STEP_MB = 16
MEMORY_CAP_MB = 4096
MEMORY_TIMEOUT_S = 30.0
FORK_CAP = 2000
FORK_LOOP_S = 20
FORK_CHILD_SLEEP_S = 30
FORK_TIMEOUT_S = 45.0
CPU_SPIN_S = 10
CPU_TIMEOUT_S = 25.0

SECRET_NAME = re.compile(r"KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL", re.IGNORECASE)
SAFE_NAME = re.compile(r"[^A-Za-z0-9_.-]")
UNPRINTABLE = re.compile(r"[^\x20-\x7e]")
BLOCKED_CONNECT = {errno.ENETUNREACH, errno.EHOSTUNREACH, errno.EACCES, errno.EPERM, errno.ENETDOWN}
BLOCKED_WRITE = {errno.EROFS, errno.EACCES, errno.EPERM}

ProbeResult = tuple[str, str]


class GuardRefused(RuntimeError):
    pass


# Output


def probe_line(probe: str, phase: str, outcome: str, detail: str) -> str:
    """One line, always short: every character is ASCII, so JSON can at most double the detail."""
    short = UNPRINTABLE.sub("?", detail)[:MAX_DETAIL_CHARS]
    fields = {"probe": probe, "phase": phase, "outcome": outcome, "detail": short}
    return "FF_PROBE " + json.dumps(fields)


def emit(probe: str, phase: str, outcome: str, detail: str) -> None:
    try:
        sys.stdout.write(probe_line(probe, phase, outcome, detail) + "\n")
        sys.stdout.flush()
    except (OSError, ValueError):
        pass


# Safety guard


def guard_refusal(allowed: str | None, dockerenv_exists: bool, route_table: str | None) -> str | None:
    """Why the attacks must not run, or None if every condition holds. Fails closed: a route
    table that can't be read or doesn't look like one counts as having routes."""
    reasons = []
    if allowed != "1":
        reasons.append(f"{ALLOW_ENV} is not 1")
    if not dockerenv_exists:
        reasons.append(f"{DOCKERENV} is missing")
    if route_table is None:
        reasons.append("route table is unreadable")
    else:
        lines = route_table.splitlines()
        if not lines or not lines[0].startswith("Iface"):
            reasons.append("route table is malformed")
        elif any(line.strip() for line in lines[1:]):
            reasons.append("network routes exist")
    return "refused: " + "; ".join(reasons) if reasons else None


def _read_route_table() -> str | None:
    try:
        return Path(ROUTE_TABLE).read_text()
    except OSError:
        return None


def current_refusal() -> str | None:
    return guard_refusal(os.environ.get(ALLOW_ENV), os.path.exists(DOCKERENV), _read_route_table())


def _emit_refusal(refusal: str) -> None:
    emit("guard", "result", "inconclusive", refusal)


# Interpreting what was observed. Pure, and "blocked" always rests on a specific error.


def connect_outcome(error: OSError | None) -> ProbeResult:
    if error is None:
        return "not_blocked", "connected to 1.1.1.1:443, sent nothing"
    if isinstance(error, TimeoutError):
        return "blocked", f"timed out after {EGRESS_TIMEOUT_S:.0f}s"
    name = errno.errorcode.get(error.errno or 0, type(error).__name__)
    if error.errno in BLOCKED_CONNECT:
        return "blocked", f"connect failed: {name}"
    return "inconclusive", f"unexpected connect error: {name}"


def environ_names(raw: bytes) -> list[str]:
    """Names from a /proc/<pid>/environ blob. Values are dropped here, before anything else sees them."""
    return [
        entry.split(b"=", 1)[0].decode("ascii", "replace")
        for entry in raw.split(b"\0")
        if b"=" in entry
    ]


def secret_names(names: Iterable[str]) -> list[str]:
    return sorted({SAFE_NAME.sub("?", name)[:40] for name in names if SECRET_NAME.search(name)})


def privileges_outcome(cap_eff: str | None, setuid_error: OSError | None) -> ProbeResult:
    if cap_eff is None:
        return "inconclusive", "CapEff not readable"
    setuid = f"setuid(0) {type(setuid_error).__name__}" if setuid_error else "setuid(0) succeeded"
    detail = f"CapEff={cap_eff}, {setuid}"
    if int(cap_eff, 16) == 0 and isinstance(setuid_error, PermissionError):
        return "blocked", detail
    return "not_blocked", detail


def memory_outcome(returncode: int | None, output: str, oom_kills: int | None) -> ProbeResult:
    """oom_kills is the change in the cgroup's counter, or None if the counter can't be read."""
    lines = output.split()
    last = lines[-1] if lines else "0"
    if returncode is None:
        return "inconclusive", f"child still allocating at the timeout, last {last}"
    if returncode == 0:
        return "not_blocked", f"child reached the {MEMORY_CAP_MB} MB cap"
    if last == "memoryerror":
        reached = lines[-2] if len(lines) > 1 else "0"
        return "blocked", f"child got MemoryError after {reached} MB"
    if returncode == -signal.SIGKILL:
        if oom_kills == 0:
            return "inconclusive", f"child SIGKILLed after {last} MB, cgroup counted no OOM kill"
        seen = "unreadable" if oom_kills is None else str(oom_kills)
        return "blocked", f"child SIGKILLed after {last} MB, cgroup oom_kill delta {seen}"
    return "inconclusive", f"child exited {returncode} after {last} MB"


def fork_outcome(returncode: int | None, output: str) -> ProbeResult:
    if returncode is None:
        return "inconclusive", "fork child timed out"
    try:
        result = json.loads(output)
        count, reaped, error = int(result["count"]), int(result["reaped"]), str(result["error"])
    except (ValueError, KeyError, TypeError):
        return "inconclusive", f"fork child exited {returncode} without a result"
    detail = f"{count} children, {reaped} reaped"
    if error == "BlockingIOError":
        return "blocked", f"BlockingIOError after {detail}"
    if error == "cap":
        return "not_blocked", f"reached the hard cap of {FORK_CAP}: {detail}"
    return "inconclusive", f"stopped by {error}: {detail}"


def cpu_outcome(returncode: int | None, output: str, throttled: int | None) -> ProbeResult:
    if returncode is None:
        return "inconclusive", "spin child timed out"
    if returncode < 0:
        return "blocked", f"spin child killed by signal {-returncode}"
    try:
        wall_s, cpu_s = (float(value) for value in output.split())
    except ValueError:
        return "inconclusive", f"spin child exited {returncode} without a result"
    seen = "unreadable" if throttled is None else str(throttled)
    detail = f"{wall_s:.1f}s wall, {cpu_s:.1f}s cpu, nr_throttled delta {seen}"
    if throttled and cpu_s < 0.8 * wall_s:
        return "blocked", f"throttled: {detail}"
    return "not_blocked", f"ended by itself: {detail}"


# Child processes, so a kill takes the child and not this server


MEMORY_CHILD = f"""
import sys
try:
    with open("/proc/self/oom_score_adj", "w") as adj:
        adj.write("1000")
except OSError:
    pass
held, total = [], 0
try:
    while total < {MEMORY_CAP_MB}:
        held.append(bytearray(b"\\x01") * ({MEMORY_STEP_MB} * 1024 * 1024))
        total += {MEMORY_STEP_MB}
        print(total, flush=True)
except MemoryError:
    print("memoryerror", flush=True)
    sys.exit(3)
"""

FORK_CHILD = f"""
import json, os, signal, time
pids, error = [], "cap"
deadline = time.monotonic() + {FORK_LOOP_S}
try:
    while len(pids) < {FORK_CAP}:
        if time.monotonic() >= deadline:
            error = "deadline"
            break
        pid = os.fork()
        if pid == 0:
            try:
                os.close(1)
                time.sleep({FORK_CHILD_SLEEP_S})
            finally:
                os._exit(0)
        pids.append(pid)
except OSError as caught:
    error = type(caught).__name__
except MemoryError:
    error = "MemoryError"
reaped = 0
for pid in pids:
    try:
        os.kill(pid, signal.SIGKILL)
        os.waitpid(pid, 0)
        reaped += 1
    except OSError:
        pass
print(json.dumps({{"count": len(pids), "reaped": reaped, "error": error}}), flush=True)
"""

CPU_CHILD = f"""
import time
start = time.monotonic()
while time.monotonic() - start < {CPU_SPIN_S}:
    pass
print(time.monotonic() - start, time.process_time(), flush=True)
"""


def _run_child(code: str, timeout_s: float) -> tuple[int | None, str]:
    """The only way to start a child. Returns (None, output) if it had to be killed at the timeout."""
    refusal = current_refusal()
    if refusal:
        raise GuardRefused(refusal)
    process = subprocess.Popen(
        [sys.executable, "-S", "-c", code],
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
    )
    try:
        output, _ = process.communicate(timeout=timeout_s)
        return process.returncode, output
    except subprocess.TimeoutExpired:
        process.kill()
        output, _ = process.communicate()
        return None, output


def _cgroup_counter(path: str, name: str) -> int | None:
    try:
        for line in Path(path).read_text().splitlines():
            key, _, value = line.partition(" ")
            if key == name:
                return int(value)
    except (OSError, ValueError):
        pass
    return None


def _delta(before: int | None, after: int | None) -> int | None:
    return None if before is None or after is None else after - before


# Probes


def probe_egress_external() -> ProbeResult:
    try:
        with socket.create_connection(("1.1.1.1", 443), timeout=EGRESS_TIMEOUT_S):
            pass
    except OSError as error:
        return connect_outcome(error)
    return connect_outcome(None)


def probe_egress_dns() -> ProbeResult:
    """getaddrinfo has no timeout of its own, so it runs in a thread that may be abandoned."""
    results: list[ProbeResult] = []

    def resolve() -> None:
        try:
            socket.getaddrinfo("example.com", 443)
            results.append(("not_blocked", "example.com resolved"))
        except socket.gaierror as error:
            results.append(("blocked", f"resolution failed: {error.strerror}"))
        except OSError as error:
            results.append(("inconclusive", f"unexpected error: {type(error).__name__}"))

    thread = threading.Thread(target=resolve, daemon=True)
    thread.start()
    thread.join(EGRESS_TIMEOUT_S)
    return results[0] if results else ("blocked", f"no answer within {EGRESS_TIMEOUT_S:.0f}s")


def probe_secrets_env() -> ProbeResult:
    found = secret_names(os.environ)
    try:
        init_found = secret_names(environ_names(Path("/proc/1/environ").read_bytes()))
        init_note = f"/proc/1/environ: {len(init_found)} names"
    except OSError as error:
        init_found = []
        init_note = f"/proc/1/environ unreadable ({type(error).__name__})"
    names = sorted(set(found) | set(init_found))
    if names:
        return "not_blocked", f"names found: {','.join(names)}"
    return "blocked", f"no secret names in the environment; {init_note}"


def probe_docker_socket() -> ProbeResult:
    if os.path.lexists("/var/run/docker.sock"):
        return "not_blocked", "/var/run/docker.sock exists"
    return "blocked", "/var/run/docker.sock does not exist"


def probe_write_outside_tmp() -> ProbeResult:
    notes, wrote, unknown = [], False, False
    for directory in ("/app", "/etc", "/usr"):
        path = f"{directory}/.ff_probe_{os.getpid()}"
        try:
            os.close(os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600))
        except OSError as error:
            notes.append(f"{directory}:{errno.errorcode.get(error.errno or 0, type(error).__name__)}")
            unknown = unknown or error.errno not in BLOCKED_WRITE
        else:
            wrote = True
            notes.append(f"{directory}:WROTE")
            os.unlink(path)
    outcome = "not_blocked" if wrote else "inconclusive" if unknown else "blocked"
    return outcome, " ".join(notes)


def probe_privileges() -> ProbeResult:
    cap_eff = None
    try:
        for line in Path("/proc/self/status").read_text().splitlines():
            if line.startswith("CapEff:"):
                cap_eff = line.split()[1]
    except (OSError, IndexError):
        pass
    setuid_error = None
    try:
        os.setuid(0)
    except OSError as error:
        setuid_error = error
    return privileges_outcome(cap_eff, setuid_error)


def probe_memory() -> ProbeResult:
    events = "/sys/fs/cgroup/memory.events"
    before = _cgroup_counter(events, "oom_kill")
    returncode, output = _run_child(MEMORY_CHILD, MEMORY_TIMEOUT_S)
    return memory_outcome(returncode, output, _delta(before, _cgroup_counter(events, "oom_kill")))


def probe_fork() -> ProbeResult:
    return fork_outcome(*_run_child(FORK_CHILD, FORK_TIMEOUT_S))


def probe_cpu_spin() -> ProbeResult:
    stats = "/sys/fs/cgroup/cpu.stat"
    before = _cgroup_counter(stats, "nr_throttled")
    returncode, output = _run_child(CPU_CHILD, CPU_TIMEOUT_S)
    return cpu_outcome(returncode, output, _delta(before, _cgroup_counter(stats, "nr_throttled")))


# Survivable ones first: a limit may end the process during the last three.
PROBES: list[tuple[str, Callable[[], ProbeResult]]] = [
    ("egress_external", probe_egress_external),
    ("egress_dns", probe_egress_dns),
    ("secrets_env", probe_secrets_env),
    ("docker_socket", probe_docker_socket),
    ("write_outside_tmp", probe_write_outside_tmp),
    ("privileges", probe_privileges),
    ("memory", probe_memory),
    ("fork", probe_fork),
    ("cpu_spin", probe_cpu_spin),
]


def run_attacks() -> None:
    refusal = current_refusal()
    if refusal:
        _emit_refusal(refusal)
        return
    for name, probe in PROBES:
        emit(name, "started", "inconclusive", "attempting")
        try:
            outcome, detail = probe()
        except Exception as error:
            outcome, detail = "inconclusive", f"probe error: {type(error).__name__}"
        emit(name, "result", outcome, detail)


_attacks_lock = threading.Lock()
_attacks_thread: threading.Thread | None = None


def start_attacks() -> threading.Thread:
    """Starts the attacks at most once, away from the event loop so the hooks keep answering."""
    global _attacks_thread
    with _attacks_lock:
        if _attacks_thread is None:
            _attacks_thread = threading.Thread(target=run_attacks, daemon=True, name="ff-hostile")
            _attacks_thread.start()
        return _attacks_thread


# The target contract. Every handler is async: a thread pool may be unable to start a thread
# while the fork attack holds the pids limit.


class ResetRequest(BaseModel):
    stock: int = Field(ge=0)


def _build_id() -> str:
    return hashlib.sha256(Path(__file__).read_bytes()).hexdigest()[:16]


BUILD = {
    "build_id": _build_id(),
    "instance_id": uuid.uuid4().hex,
    "instance_started_at": datetime.now(UTC).isoformat(),
    "stale_source": False,
}

app = FastAPI(title="hostile-build")
app.state.stock = 0
app.state.in_flight = 0


def _snapshot() -> dict[str, object]:
    return {
        "stock": app.state.stock,
        "orders_confirmed": 0,
        "payments_unattached": 0,
        "orders_without_payment": 0,
        "in_flight": app.state.in_flight,
        "stale_source": False,
        "build": BUILD,
    }


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/__ff/state")
async def state() -> dict[str, object]:
    return _snapshot()


@app.post("/__ff/reset")
async def reset(body: ResetRequest) -> dict[str, object]:
    app.state.stock = body.stock
    return _snapshot()


@app.post("/reserve")
async def reserve(request: Request) -> Response:
    """Never answers while the client is there, so the verdict can't be PASS. It stops counting
    as in flight once the client goes, so the runner's settle step still works."""
    start_attacks()
    app.state.in_flight += 1
    try:
        await request.body()
        while not await request.is_disconnected():
            await asyncio.sleep(DISCONNECT_POLL_S)
    finally:
        app.state.in_flight -= 1
    return Response(status_code=503)


_import_refusal = current_refusal()
if _import_refusal:
    _emit_refusal(_import_refusal)
