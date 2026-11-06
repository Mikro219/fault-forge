"""Runs one k6 trial under a hard deadline and normalizes its output into RequestRecords.

All k6 specifics stay in this module. k6 runs from a fixed script, with an argument list
(never a shell), a minimal environment, and its own process group. docs/VERIFIED.md records
how each option behaves on the pinned version.
"""

import hashlib
import json
import os
import shutil
import signal
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from faultforge.evidence import peak_in_flight
from faultforge.models import (
    IncompleteReason,
    LoadConfig,
    OutcomeConfig,
    RequestRecord,
    RequestTemplate,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_DIR = Path(__file__).resolve().parent / "scripts"
SCRIPTS = {"burst_request": SCRIPT_DIR / "burst_request.js"}

# Every address range except 127.0.0.0/8, including all of IPv6.
BLOCKED_IP_RANGES = (
    "0.0.0.0/2",
    "64.0.0.0/3",
    "96.0.0.0/4",
    "112.0.0.0/5",
    "120.0.0.0/6",
    "124.0.0.0/7",
    "126.0.0.0/8",
    "128.0.0.0/1",
    "::/0",
)
SYSTEM_TAGS = "status,method,name,expected_response,error_code"
EVIDENCE_METRIC = "ff_req"
SAFE_PATH = "/usr/bin:/bin"

KILL_GRACE_S = 3.0
TERM_TO_KILL_S = 3.0
LOG_TAIL_BYTES = 200_000


class K6Missing(Exception):
    """The k6 binary can't be found."""


class UnknownScript(Exception):
    """The scenario names a script that isn't in the built-in library."""


@dataclass(frozen=True)
class TrialSpec:
    origin: str
    request: RequestTemplate
    outcomes: OutcomeConfig
    load: LoadConfig
    trial: int
    kind: Literal["concurrent", "serial_control"] = "concurrent"
    script: str = "burst_request"


@dataclass(frozen=True)
class EngineResult:
    pid: int
    exit_code: int | None
    killed: bool
    iterations_completed: int
    iterations_dropped: int
    records: tuple[RequestRecord, ...]
    peak_in_flight: int
    incomplete_reason: IncompleteReason | None


def find_k6() -> Path:
    configured = os.environ.get("FF_K6_PATH")
    candidates = [configured, REPO_ROOT / ".tools" / "bin" / "k6", shutil.which("k6")]
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return Path(candidate).resolve()
    raise K6Missing("k6 not found; run scripts/install_k6.sh or set FF_K6_PATH")


def script_path(name: str) -> Path:
    if name not in SCRIPTS:
        raise UnknownScript(name)
    return SCRIPTS[name]


def script_sha256(name: str) -> str:
    return hashlib.sha256(script_path(name).read_bytes()).hexdigest()


def k6_version(k6: Path) -> str:
    output = subprocess.run(
        [str(k6), "version"], env=build_env(Path(tempfile.gettempdir())),
        capture_output=True, text=True, check=True, timeout=10,
    ).stdout
    return output.split()[1].removeprefix("v")


def build_env(scratch: Path) -> dict[str, str]:
    """Nothing is inherited, so ambient K6_* settings and tokens can't reach k6."""
    return {"PATH": SAFE_PATH, "HOME": str(scratch), "TMPDIR": str(scratch)}


def build_spec(spec: TrialSpec, summary_path: Path) -> dict[str, object]:
    return {
        "url": f"{spec.origin}{spec.request.path}",
        "method": spec.request.method,
        "json": spec.request.json_body,
        "accepted_status": spec.outcomes.accepted_status,
        "rejected_status": spec.outcomes.rejected_status,
        "capture_field": spec.outcomes.capture_field,
        "vus": spec.load.vus,
        "iterations": spec.load.iterations,
        "max_duration_s": spec.load.max_duration_s,
        "trial": spec.trial,
        "summary_path": str(summary_path),
    }


def build_argv(k6: Path, spec: TrialSpec, raw_path: Path, summary_path: Path) -> list[str]:
    return [
        str(k6),
        "run",
        "--no-usage-report",
        "--quiet",
        "--include-system-env-vars=false",
        f"--blacklist-ip={','.join(BLOCKED_IP_RANGES)}",
        f"--system-tags={SYSTEM_TAGS}",
        f"--out=json={raw_path}",
        "--log-format=json",
        "-e",
        f"FF_SPEC={json.dumps(build_spec(spec, summary_path))}",
        str(script_path(spec.script)),
    ]


def _stop_process_group(process: subprocess.Popen) -> None:
    for sig, wait_s in ((signal.SIGTERM, TERM_TO_KILL_S), (signal.SIGKILL, None)):
        try:
            os.killpg(process.pid, sig)
        except ProcessLookupError:
            break
        try:
            process.wait(timeout=wait_s)
            break
        except subprocess.TimeoutExpired:
            continue
    process.wait()
    # A descendant can outlive the leader. It's still in the same group.
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def _record_from_point(point: dict) -> RequestRecord:
    tags = point["tags"]
    return RequestRecord(
        trial=int(tags["ff_trial"]),
        request_id=tags["ff_request_id"],
        vu=int(tags["ff_vu"]),
        iter=int(tags["ff_iter"]),
        t_start=float(tags["ff_t_start"]),
        t_end=float(tags["ff_t_end"]),
        latency_ms=point["value"],
        status=int(tags["ff_status"]),
        outcome=tags["ff_outcome"],
        captured=tags.get("ff_captured") or None,
    )


def read_records(raw_path: Path) -> list[RequestRecord]:
    """A killed k6 can leave a truncated final line, which isn't evidence and is skipped."""
    records = []
    if not raw_path.exists():
        return records
    with raw_path.open(encoding="utf-8") as raw:
        for line in raw:
            try:
                sample = json.loads(line)
            except json.JSONDecodeError:
                continue
            if sample.get("type") == "Point" and sample.get("metric") == EVIDENCE_METRIC:
                records.append(_record_from_point(sample["data"]))
    return records


def _metric_count(summary: dict, metric: str) -> int:
    return int(summary.get("metrics", {}).get(metric, {}).get("values", {}).get("count", 0))


def _read_summary(summary_path: Path) -> dict:
    try:
        return json.loads(summary_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _cap_log(log_path: Path) -> None:
    size = log_path.stat().st_size
    if size <= LOG_TAIL_BYTES:
        return
    with log_path.open("rb") as log:
        log.seek(size - LOG_TAIL_BYTES)
        tail = log.read()
    log_path.write_bytes(tail)


def classify(
    spec: TrialSpec,
    exit_code: int | None,
    killed: bool,
    iterations_completed: int,
    iterations_dropped: int,
    request_count: int,
    peak: int,
) -> IncompleteReason | None:
    """Exit code 0 doesn't mean the trial completed: k6 also exits 0 at maxDuration."""
    if killed:
        return "timeout"
    if exit_code != 0:
        return "engine_exit_nonzero"
    if iterations_completed < spec.load.iterations or iterations_dropped > 0:
        return "iterations_short"
    if request_count != spec.load.iterations:
        return "request_count_mismatch"
    if spec.kind == "concurrent" and peak < spec.load.effective_min_peak_in_flight:
        return "insufficient_contention"
    return None


def run_trial(
    spec: TrialSpec,
    trial_dir: Path,
    requests_path: Path,
    k6: Path | None = None,
    kill_after_s: float | None = None,
) -> EngineResult:
    """Runs one trial, appending its evidence to requests_path. The raw k6 output is deleted."""
    k6 = k6 or find_k6()
    trial_dir.mkdir(parents=True, exist_ok=True)
    summary_path = trial_dir / "k6-summary.json"
    log_path = trial_dir / "k6.log"
    deadline_s = spec.load.max_duration_s + KILL_GRACE_S if kill_after_s is None else kill_after_s

    with tempfile.TemporaryDirectory(dir=trial_dir, prefix=".k6-") as scratch_name:
        scratch = Path(scratch_name)
        raw_path = scratch / "raw.json"
        argv = build_argv(k6, spec, raw_path, summary_path)
        killed = False
        with log_path.open("wb") as log:
            process = subprocess.Popen(
                argv,
                env=build_env(scratch),
                cwd=scratch,
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
            try:
                process.wait(timeout=deadline_s)
            except subprocess.TimeoutExpired:
                killed = True
            finally:
                _stop_process_group(process)
        records = read_records(raw_path)

    _cap_log(log_path)
    summary = _read_summary(summary_path)
    with requests_path.open("a", encoding="utf-8") as output:
        for record in records:
            output.write(record.model_dump_json() + "\n")

    completed = _metric_count(summary, "iterations")
    dropped = _metric_count(summary, "dropped_iterations")
    peak = peak_in_flight(records)
    return EngineResult(
        pid=process.pid,
        exit_code=process.returncode,
        killed=killed,
        iterations_completed=completed,
        iterations_dropped=dropped,
        records=tuple(records),
        peak_in_flight=peak,
        incomplete_reason=classify(
            spec, process.returncode, killed, completed, dropped, len(records), peak
        ),
    )
