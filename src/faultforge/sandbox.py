"""Runs one scenario in a new pair of disposable containers (docs/PLAN.md V3.2 and V3.3).

The target is untrusted: no network, no secrets, no access to the artifacts. The runner joins
its network namespace, so 127.0.0.1 reaches the target and nothing else exists. Facts about
containment come from docker inspect and the host. Anything the target says, including its
logs and FF_PROBE lines, is untrusted and labelled as such.

Two facts don't depend on the target at all. deployed_code_sha256 is computed here from the files
written for it, and state_verification reads its database directly instead of asking its hooks.

All docker calls use argument lists, never a shell, with a timeout and a minimal environment.
Every removal acts only on containers found through the faultforge.sandbox label. The caller's
project_dir is mounted writable in the runner, so it should hold only FaultForge's own files.
"""

import hashlib
import json
import os
import re
import secrets
import shutil
import stat
import subprocess
import tempfile
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from types import MappingProxyType
from typing import Literal

from pydantic import Field

from faultforge import config as config_module, report as report_module, safety
from faultforge.cli import EXIT_CODES
from faultforge.errors import FaultForgeError
from faultforge.lifecycle import RunOptions
from faultforge.models import Contract, Report

LABEL = "faultforge.sandbox"
ROLE_LABEL = "faultforge.role"
EXPIRES_LABEL = "faultforge.expires"
SANDBOX_ORIGIN = "http://127.0.0.1:8000"
TARGET_USER = "65534:65534"
TARGET_APP_NAME = "app.py"
TARGET_DB_PATH = "/tmp/ticket.db"

# Inherited by the docker CLI so it can find its daemon. Nothing else reaches it.
DOCKER_ENV_ALLOWLIST = (
    "PATH", "HOME", "DOCKER_HOST", "DOCKER_CONTEXT", "DOCKER_CONFIG",
    "DOCKER_CERT_PATH", "DOCKER_TLS_VERIFY", "XDG_RUNTIME_DIR",
)
DEFAULT_PATH = "/usr/local/bin:/usr/bin:/bin"

MAX_APP_SOURCE_BYTES = 1024 * 1024
MAX_CODE_BYTES = 8 * 1024 * 1024
MAX_REPORT_BYTES = 16 * 1024 * 1024
MAX_RUNNER_STDOUT_BYTES = 16 * 1024 * 1024
MAX_PROBE_SCAN_BYTES = 1024 * 1024
MAX_PROBES = 64
MAX_PROBE_LINE_BYTES = 512
PROBE_PREFIX = b"FF_PROBE "
LOG_TAIL_BYTES = 8 * 1024
MAX_DETAIL_CHARS = 2000
REMOVE_ATTEMPTS = 3
UNPRINTABLE = re.compile(r"[^\x20-\x7e\n\t]")
EXPIRES_VALUE = re.compile(r"[0-9]{1,12}")
STATE_FIELD_NAME = re.compile(r"[a-z][a-z0-9_]{0,47}")
STATE_EXEC_TIMEOUT_S = 10.0
MAX_STATE_OUTPUT_BYTES = 64 * 1024
MAX_STATE_AFTER_BYTES = 1024 * 1024
MAX_STATE_FIELDS = 32
MAX_STATE_VALUE = 2**63
STATE_AFTER_NAME = "state-after.json"

# The same definitions as state() in demo/ticket-api/ticket_support/db.py, held here because the
# target's own copy can't be trusted. tests/test_sandbox.py keeps the two in step.
TICKET_API_STATE_VERIFIER: Mapping[str, str] = MappingProxyType({
    "stock": "SELECT COALESCE(SUM(stock), 0) FROM items",
    "orders_confirmed": "SELECT COUNT(*) FROM orders",
    "payments_unattached": (
        "SELECT COUNT(*) FROM payments WHERE status IN ('authorized', 'void_pending') "
        "AND id NOT IN (SELECT payment_id FROM orders WHERE payment_id IS NOT NULL)"
    ),
    "orders_without_payment": (
        "SELECT COUNT(*) FROM orders WHERE payment_id IS NULL "
        "OR payment_id NOT IN (SELECT id FROM payments WHERE status = 'authorized')"
    ),
})

# Fixed text that takes its inputs as arguments. It runs in the target's container, so -I keeps
# PYTHONPATH and the working directory out of sys.path, and only the standard library is imported.
STATE_VERIFIER_SCRIPT = """\
import json, sqlite3, sys
connection = sqlite3.connect(sys.argv[1], uri=True, timeout=2, isolation_level=None)
connection.execute("PRAGMA query_only=ON")
connection.execute("PRAGMA trusted_schema=OFF")
queries = json.loads(sys.argv[2])
print(json.dumps({name: connection.execute(sql).fetchone()[0] for name, sql in queries.items()}))
"""

FailureReason = Literal[
    "invalid_spec",
    "docker_unavailable",
    "image_missing",
    "target_start_failed",
    "target_exited",
    "runner_start_failed",
    "target_unreachable",
    "not_comparable",
    "runner_error",
    "runner_oom",
    "report_missing",
    "report_invalid",
    "wall_limit_exceeded",
    "internal_error",
]


@dataclass(frozen=True)
class ContainerLimits:
    memory_mb: int
    cpus: float
    pids: int
    tmpfs_mb: int
    log_mb: int


@dataclass(frozen=True)
class SandboxPolicy:
    """Every number that bounds a sandbox. wall_limit_s covers the run budget (60 s), the wait for
    the target (at most 30 s) and startup. expiry_margin_s covers collecting and removing after
    that, 13 docker calls at most. stale_after_s applies only to a container with no usable expiry
    label, and must exceed the wall limit too."""

    target: ContainerLimits = ContainerLimits(memory_mb=256, cpus=1.0, pids=128, tmpfs_mb=64, log_mb=4)
    runner: ContainerLimits = ContainerLimits(memory_mb=512, cpus=2.0, pids=512, tmpfs_mb=64, log_mb=16)
    wall_limit_s: float = 100.0
    expiry_margin_s: float = 400.0
    stale_after_s: float = 300.0
    wait_for_target_s: float = 15.0
    docker_timeout_s: float = 30.0
    target_image: str = "faultforge-target:dev"
    runner_image: str = "faultforge-runner:dev"
    docker: str = "docker"


DEFAULT_POLICY = SandboxPolicy()


@dataclass(frozen=True, kw_only=True)
class TargetSpec:
    """app_source is bytes so that generated code never has to touch a host file before this."""

    app_source: bytes
    config_path: Path
    support_dir: Path | None = None
    allow_hostile: bool = False
    # State field name to a fixed SQL query for its value. None means no direct check.
    state_verifier: Mapping[str, str] | None = field(default=None, hash=False)

    def __post_init__(self) -> None:
        if self.state_verifier is not None:
            object.__setattr__(self, "state_verifier", MappingProxyType(dict(self.state_verifier)))


class SandboxError(Exception):
    def __init__(self, reason: FailureReason, detail: str = ""):
        super().__init__(f"{reason}: {detail}")
        self.reason: FailureReason = reason
        self.detail = detail


class _DockerTimeout(SandboxError):
    pass


class MountFact(Contract):
    destination: str
    read_only: bool


class ContainerFacts(Contract):
    """Read from docker inspect. Env holds names only: values never leave this module."""

    container_id: str
    running_when_inspected: bool
    network_mode: str
    read_only_rootfs: bool
    cap_drop: list[str]
    cap_add: list[str]
    privileged: bool
    no_new_privileges: bool
    user: str
    memory_bytes: int
    memory_swap_bytes: int
    nano_cpus: int
    pids_limit: int | None
    oom_killed: bool
    exit_code: int | None = Field(description="None if it was still running when inspected")
    env_names: list[str]
    mounts: list[MountFact]
    limits_applied: bool = Field(description="Every setting above matches the policy")


class StateMismatch(Contract):
    name: str
    reported: int | None = Field(description="From state-after.json. None if it isn't a valid integer")
    database: int


class StateVerification(Contract):
    """The final trial's reported state against the database, read directly by the host."""

    status: Literal["matched", "mismatch", "unavailable"]
    mismatches: list[StateMismatch] = Field(default_factory=list)
    reason: str | None = Field(default=None, description="Why it's unavailable, from a fixed vocabulary")


class ContainmentFacts(Contract):
    target: ContainerFacts | None = None
    runner: ContainerFacts | None = None
    containers_left: int | None = Field(default=None, description="None if docker couldn't be asked")
    wall_s: float = Field(ge=0)
    wall_limit_s: float
    probes: list[dict[str, object]] = Field(
        default_factory=list, description="What the target says it did. Every entry is self_reported."
    )
    probes_without_result: list[str] = Field(
        default_factory=list, description="Started but never finished: killed, or removed with the container"
    )
    target_log_tail: str = Field(default="", description="Untrusted text from the target")
    services: list[ContainerFacts] = Field(default_factory=list, description="A stack's other containers, read by the host")
    state_verification: StateVerification | None = Field(default=None, description="None if not requested")


class SandboxResult(Contract):
    status: Literal["completed", "failed"]
    failure_reason: FailureReason | None = None
    failure_detail: str | None = Field(default=None, description="May quote untrusted text")
    report: Report | None = None
    containment: ContainmentFacts
    deployed_code_sha256: str | None = Field(
        default=None, description="Of the files the host wrote for the target. Not reported by the target."
    )


# Docker calls


def docker_env() -> dict[str, str]:
    env = {name: os.environ[name] for name in DOCKER_ENV_ALLOWLIST if name in os.environ}
    env.setdefault("PATH", DEFAULT_PATH)
    return env


def _docker(
    policy: SandboxPolicy, args: list[str], timeout_s: float | None = None, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
) -> subprocess.CompletedProcess:
    timeout = policy.docker_timeout_s if timeout_s is None else max(timeout_s, 0.1)
    try:
        return subprocess.run(
            [policy.docker, *args], env=docker_env(), stdin=subprocess.DEVNULL,
            stdout=stdout, stderr=stderr, timeout=timeout, check=False,
        )
    except subprocess.TimeoutExpired as error:
        raise _DockerTimeout("docker_unavailable", f"docker {args[0]} exceeded {timeout:g} s") from error
    except OSError as error:
        raise SandboxError("docker_unavailable", f"can't run {policy.docker}: {type(error).__name__}") from error


def _stderr(result: subprocess.CompletedProcess) -> str:
    return _clean((result.stderr or b"").decode("utf-8", "replace"))


def _clean(text: str, limit: int = MAX_DETAIL_CHARS) -> str:
    """Keeps the end of the text and removes anything a terminal or page could act on."""
    return UNPRINTABLE.sub("?", text)[-limit:]


def _list_labelled(policy: SandboxPolicy, label_filter: str) -> list[str]:
    result = _docker(policy, ["ps", "--all", "--quiet", "--no-trunc", "--filter", f"label={label_filter}"])
    if result.returncode != 0:
        raise SandboxError("docker_unavailable", _stderr(result))
    return result.stdout.decode("ascii", "replace").split()


def count_labelled(policy: SandboxPolicy = DEFAULT_POLICY) -> int:
    """How many sandbox containers exist now, in any state. Raises SandboxError if docker can't be used."""
    return len(_list_labelled(policy, LABEL))


def _created_at(text: str) -> datetime | None:
    """Docker's timestamps have nanoseconds, which datetime can't hold."""
    trimmed = re.sub(r"(\.\d{6})\d*", r"\1", text.strip()).replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(trimmed)
    except ValueError:
        return None


def expiry_unix(started_wall: float, policy: SandboxPolicy) -> int:
    return int(started_wall + policy.wall_limit_s + policy.expiry_margin_s) + 1


def _is_stale(labels: dict, created: datetime | None, now: datetime, max_age_s: float) -> bool:
    """A usable expiry label decides alone, so a live container is never removed for its age. Any
    other label value falls back to the age rule, so containers without one still get cleaned."""
    expires = labels.get(EXPIRES_LABEL)
    if isinstance(expires, str) and EXPIRES_VALUE.fullmatch(expires):
        return now.timestamp() >= int(expires)
    return created is not None and (now - created).total_seconds() >= max_age_s


def reap_stale(policy: SandboxPolicy = DEFAULT_POLICY, max_age_s: float | None = None) -> int:
    """Removes labelled containers past their expiry and returns how many. max_age_s applies only to
    those with no usable expiry label. Raises SandboxError if docker can't be used. A container that
    has no sandbox label is never looked at or removed."""
    max_age_s = policy.stale_after_s if max_age_s is None else max_age_s
    ids = _list_labelled(policy, LABEL)
    if not ids:
        return 0
    # A container that vanished since the list makes inspect exit nonzero; the others still print.
    described = _docker(policy, ["inspect", "--format", "{{.Id}} {{.Created}} {{json .Config.Labels}}", *ids])
    stale = []
    now = datetime.now(UTC)
    for line in described.stdout.decode("utf-8", "replace").splitlines():
        parts = line.split(" ", 2)
        if len(parts) != 3 or parts[0] not in ids:
            continue
        try:
            labels = json.loads(parts[2])
        except (ValueError, RecursionError):
            continue
        if isinstance(labels, dict) and LABEL in labels and _is_stale(labels, _created_at(parts[1]), now, max_age_s):
            stale.append(parts[0])
    if stale:
        _docker(policy, ["rm", "--force", "--volumes", *stale])
    return len(stale)


def _remove_sandbox(policy: SandboxPolicy, sandbox_id: str) -> int:
    """Finds by label rather than by name, so it also catches a container whose docker run timed
    out. Returns how many are left."""
    label_filter = f"{LABEL}={sandbox_id}"
    for _ in range(REMOVE_ATTEMPTS):
        ids = _list_labelled(policy, label_filter)
        if not ids:
            return 0
        try:
            _docker(policy, ["rm", "--force", "--volumes", *ids])
        except _DockerTimeout:
            continue
    return len(_list_labelled(policy, label_filter))


# Container commands


def _mount(source: Path, destination: str, read_only: bool) -> str:
    # --mount is comma-separated, and -v is colon-separated.
    if any(char in str(source) for char in ",\n\"'"):
        raise SandboxError("invalid_spec", f"can't mount a path containing a comma or quote: {source}")
    return f"type=bind,src={source},dst={destination}" + (",readonly" if read_only else "")


def _common_args(name: str, sandbox_id: str, role: str, limits: ContainerLimits, expires_at: int) -> list[str]:
    """--ipc none also removes /dev/shm, which --read-only would leave writable."""
    memory = f"{limits.memory_mb}m"
    return [
        "run", "--detach", "--pull", "never", "--name", name,
        "--label", f"{LABEL}={sandbox_id}", "--label", f"{ROLE_LABEL}={role}",
        "--label", f"{EXPIRES_LABEL}={expires_at}",
        "--read-only",
        "--tmpfs", f"/tmp:rw,noexec,nosuid,nodev,size={limits.tmpfs_mb}m,mode=1777",
        "--cap-drop", "ALL",
        "--security-opt", "no-new-privileges",
        "--ipc", "none",
        "--memory", memory, "--memory-swap", memory,
        "--cpus", f"{limits.cpus:g}",
        "--pids-limit", str(limits.pids),
        "--log-driver", "json-file",
        "--log-opt", f"max-size={limits.log_mb}m", "--log-opt", "max-file=1",
    ]


def _env_args(env: dict[str, str]) -> list[str]:
    """Always NAME=value. A bare -e NAME would copy the value from the docker CLI's environment."""
    return [argument for name, value in env.items() for argument in ("--env", f"{name}={value}")]


def target_env(allow_hostile: bool) -> dict[str, str]:
    env = {
        "FF_TEST_HOOKS": "1",
        "FF_DB_PATH": TARGET_DB_PATH,
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONPATH": "/app",
    }
    if allow_hostile:
        env["FF_HOSTILE_ALLOWED"] = "1"
    return env


RUNNER_ENV = {"PYTHONDONTWRITEBYTECODE": "1", "HOME": "/tmp"}


def target_argv(
    name: str, sandbox_id: str, code_dir: Path, allow_hostile: bool, policy: SandboxPolicy,
    expires_at: int | None = None,
) -> list[str]:
    expires_at = expiry_unix(time.time(), policy) if expires_at is None else expires_at
    return [
        *_common_args(name, sandbox_id, "target", policy.target, expires_at),
        "--network", "none",
        "--user", TARGET_USER,
        "--mount", _mount(code_dir, "/app", read_only=True),
        "--workdir", "/app",
        *_env_args(target_env(allow_hostile)),
        policy.target_image,
        # -u because a hostile or crashing process must not lose its last lines in a buffer.
        "python", "-u", "-m", "uvicorn", "app:app",
        "--host", "127.0.0.1", "--port", "8000", "--workers", "1", "--no-access-log",
    ]


def runner_argv(
    name: str, sandbox_id: str, target_name: str, project_dir: Path, scenario_id: str,
    options: RunOptions, policy: SandboxPolicy, expires_at: int | None = None,
) -> list[str]:
    expires_at = expiry_unix(time.time(), policy) if expires_at is None else expires_at
    return [
        *_common_args(name, sandbox_id, "runner", policy.runner, expires_at),
        "--network", f"container:{target_name}",
        "--user", _runner_user(),
        "--mount", _mount(project_dir, "/out", read_only=False),
        "--workdir", "/out",
        *_env_args(RUNNER_ENV),
        policy.runner_image,
        "faultforge", "--project", "/out", "run", scenario_id, "--json",
        f"--wait-for-target-s={policy.wait_for_target_s:g}",
        *_option_args(options),
    ]


def _runner_user() -> str:
    """The artifacts must be readable on the host, so the runner takes the host's identity."""
    uid, gid = os.getuid(), os.getgid()
    if uid == 0 or gid == 0:
        raise SandboxError("invalid_spec", "refusing to run the runner as root; start FaultForge as a non-root user")
    return f"{uid}:{gid}"


def state_verifier_argv(target_name: str, queries: Mapping[str, str]) -> list[str]:
    return [
        "exec", "--workdir", "/", target_name,
        "python", "-I", "-c", STATE_VERIFIER_SCRIPT, f"file:{TARGET_DB_PATH}?mode=ro", json.dumps(dict(queries)),
    ]


def _option_args(options: RunOptions) -> list[str]:
    args = []
    for flag, value in (
        ("--trials", options.trials),
        ("--vus", options.vus),
        ("--iterations", options.iterations),
        ("--max-duration-s", options.max_duration_s),
    ):
        if value is not None:
            args.append(f"{flag}={int(value)}")
    if options.serial_control is not None:
        args.append("--serial-control" if options.serial_control else "--no-serial-control")
    if options.compare_to is not None:
        args.append(f"--compare-to={safety.validate_run_id(options.compare_to)}")
    return args


# Preparing the files


def write_code_dir(spec: TargetSpec) -> Path:
    """A new directory, readable by the target's user, with no bytecode and no symlinks."""
    if not spec.app_source or len(spec.app_source) > MAX_APP_SOURCE_BYTES:
        raise SandboxError("invalid_spec", f"app_source must be 1 to {MAX_APP_SOURCE_BYTES} bytes")
    root = Path(tempfile.mkdtemp(prefix="faultforge-code-"))
    try:
        os.chmod(root, 0o755)
        (root / TARGET_APP_NAME).write_bytes(spec.app_source)
        os.chmod(root / TARGET_APP_NAME, 0o644)
        if spec.support_dir is not None:
            _copy_support(Path(spec.support_dir), root)
    except BaseException:
        shutil.rmtree(root, ignore_errors=True)
        raise
    return root


def _copy_support(source: Path, root: Path) -> None:
    if source.is_symlink() or not source.is_dir():
        raise SandboxError("invalid_spec", f"support_dir must be a real directory: {source}")
    if not source.name.isidentifier():
        raise SandboxError("invalid_spec", f"support_dir must be importable: {source.name}")
    total = 0
    for directory, subdirs, files in os.walk(source, followlinks=False):
        subdirs[:] = [name for name in subdirs if name != "__pycache__"]
        relative = Path(directory).relative_to(source)
        destination = root / source.name / relative
        destination.mkdir(mode=0o755)
        os.chmod(destination, 0o755)
        for name in subdirs:
            if (Path(directory) / name).is_symlink():
                raise SandboxError("invalid_spec", f"support_dir contains a symlink: {relative / name}")
        for name in files:
            path = Path(directory) / name
            info = path.lstat()
            if not stat.S_ISREG(info.st_mode):
                raise SandboxError("invalid_spec", f"support_dir contains a link or special file: {relative / name}")
            if name.endswith(".pyc"):
                continue
            total += info.st_size
            if total > MAX_CODE_BYTES:
                raise SandboxError("invalid_spec", f"support_dir is larger than {MAX_CODE_BYTES} bytes")
            shutil.copyfile(path, destination / name, follow_symlinks=False)
            os.chmod(destination / name, 0o644)


def code_dir_sha256(root: Path) -> str:
    """Sorted relative paths and bytes of every file. Lengths keep the boundaries unambiguous."""
    digest = hashlib.sha256()
    paths = sorted(
        (path.relative_to(root).as_posix(), path)
        for path in root.rglob("*")
        if path.is_file() and not path.is_symlink()
    )
    for relative, path in paths:
        name = relative.encode("utf-8")
        content = path.read_bytes()
        digest.update(len(name).to_bytes(8, "big") + name + len(content).to_bytes(8, "big") + content)
    return digest.hexdigest()


def _require_valid_verifier(queries: Mapping[str, str] | None) -> None:
    if queries is None:
        return
    if not 1 <= len(queries) <= MAX_STATE_FIELDS:
        raise SandboxError("invalid_spec", f"state_verifier must have 1 to {MAX_STATE_FIELDS} fields")
    for name, sql in queries.items():
        if not isinstance(name, str) or not STATE_FIELD_NAME.fullmatch(name):
            raise SandboxError("invalid_spec", "a state_verifier field name isn't a plain lowercase name")
        if not isinstance(sql, str) or not sql.strip():
            raise SandboxError("invalid_spec", f"state_verifier has no SQL for {name}")


def _prepare_project(spec: TargetSpec, project_dir: Path, scenario_id: str) -> None:
    """Checks the config on the host first, so a bad one never starts a container."""
    text = Path(spec.config_path).read_text(encoding="utf-8")
    config = config_module.parse_config(text)
    config_module.find_scenario(config, scenario_id)
    if config.target.origin != SANDBOX_ORIGIN:
        raise SandboxError("invalid_spec", f"a sandboxed target must use {SANDBOX_ORIGIN}")
    project_dir.mkdir(parents=True, exist_ok=True)
    destination = project_dir / config_module.CONFIG_NAME
    temp = destination.with_name(f".{destination.name}.{os.getpid()}.tmp")
    temp.write_text(text, encoding="utf-8")
    os.replace(temp, destination)


# Reading what came back


def parse_probes(stdout: bytes) -> list[dict[str, object]]:
    """Target output. A line that doesn't parse is ignored, and the target can't unmark itself."""
    probes: list[dict[str, object]] = []
    for line in stdout[:MAX_PROBE_SCAN_BYTES].splitlines():
        if len(probes) >= MAX_PROBES:
            break
        if not line.startswith(PROBE_PREFIX) or len(line) > MAX_PROBE_LINE_BYTES:
            continue
        try:
            value = json.loads(line[len(PROBE_PREFIX):], parse_constant=_refuse_constant)
            if not isinstance(value, dict):
                continue
            # A lone surrogate parses, then can't be serialized when the result is shown.
            json.dumps(value, ensure_ascii=False).encode("utf-8")
        except (ValueError, RecursionError):
            continue
        probes.append({**value, "self_reported": True})
    return probes


def _refuse_constant(name: str) -> None:
    raise ValueError(f"{name} isn't JSON")


def unfinished_probes(probes: list[dict[str, object]]) -> list[str]:
    """Probes that printed started but no result. The process was killed or removed during them."""
    named = [probe for probe in probes if isinstance(probe.get("probe"), str)]
    results = {probe["probe"] for probe in named if probe.get("phase") == "result"}
    return sorted({
        probe["probe"] for probe in named
        if probe.get("phase") == "started" and probe["probe"] not in results
    })


def _is_state_int(value: object) -> bool:
    return type(value) is int and abs(value) < MAX_STATE_VALUE


def parse_state_output(output: bytes, fields: Mapping[str, str]) -> dict[str, int]:
    """Output from inside the target's container, so untrusted: exactly the expected fields, each
    an integer, or ValueError."""
    if len(output) > MAX_STATE_OUTPUT_BYTES:
        raise ValueError("too large")
    try:
        value = json.loads(output, parse_constant=_refuse_constant)
    except RecursionError as error:
        raise ValueError("too deep") from error
    if not isinstance(value, dict) or set(value) != set(fields):
        raise ValueError("not the expected fields")
    if not all(_is_state_int(item) for item in value.values()):
        raise ValueError("not integers")
    return value


def _read_reported_state(project_dir: Path, report: Report) -> dict | None:
    """Only the last trial: a later reset would make an earlier trial's numbers differ honestly."""
    final = max(report.trials, key=lambda trial: trial.trial)
    path = report_module.run_dir(project_dir, report.run_id) / "trials" / str(final.trial) / STATE_AFTER_NAME
    if not path.is_file() or path.stat().st_size > MAX_STATE_AFTER_BYTES:
        return None
    value = json.loads(path.read_bytes(), parse_constant=_refuse_constant)
    return value if isinstance(value, dict) else None


def compare_state(database: Mapping[str, int], project_dir: Path, report: Report) -> StateVerification:
    def unavailable(reason: str) -> StateVerification:
        return StateVerification(status="unavailable", reason=reason)

    if not report.trials:
        return unavailable("no_trials")
    try:
        reported = _read_reported_state(project_dir, report)
    except (FaultForgeError, OSError, ValueError, RecursionError):
        return unavailable("state_after_unreadable")
    if reported is None:
        return unavailable("state_after_missing")
    mismatches = []
    for name, value in database.items():
        said = reported.get(name)
        said = said if _is_state_int(said) else None
        if said != value:
            mismatches.append(StateMismatch(name=name, reported=said, database=value))
    return StateVerification(status="mismatch", mismatches=mismatches) if mismatches else StateVerification(status="matched")


def _container_facts(raw: dict, limits: ContainerLimits, expected_network: str, expected_user: str) -> ContainerFacts:
    host = raw.get("HostConfig") or {}
    state = raw.get("State") or {}
    config = raw.get("Config") or {}
    running = bool(state.get("Running"))
    memory = limits.memory_mb * 1024 * 1024
    cap_drop = list(host.get("CapDrop") or [])
    cap_add = list(host.get("CapAdd") or [])
    no_new_privileges = any("no-new-privileges" in option for option in host.get("SecurityOpt") or [])
    facts = {
        "network_mode": str(host.get("NetworkMode")),
        "read_only_rootfs": bool(host.get("ReadonlyRootfs")),
        "cap_drop": cap_drop,
        "cap_add": cap_add,
        "privileged": bool(host.get("Privileged")),
        "no_new_privileges": no_new_privileges,
        "user": str(config.get("User")),
        "memory_bytes": int(host.get("Memory") or 0),
        "memory_swap_bytes": int(host.get("MemorySwap") or 0),
        "nano_cpus": int(host.get("NanoCpus") or 0),
        "pids_limit": host.get("PidsLimit"),
    }
    applied = (
        facts["network_mode"].startswith(expected_network)
        and facts["read_only_rootfs"]
        and "ALL" in cap_drop
        and not cap_add
        and not facts["privileged"]
        and no_new_privileges
        and facts["user"] == expected_user
        and not facts["user"].startswith(("0:", "root"))
        and facts["memory_bytes"] == memory
        and facts["memory_swap_bytes"] == memory
        and facts["nano_cpus"] == int(limits.cpus * 1_000_000_000)
        and facts["pids_limit"] == limits.pids
    )
    return ContainerFacts(
        container_id=str(raw.get("Id")),
        running_when_inspected=running,
        oom_killed=bool(state.get("OOMKilled")),
        exit_code=None if running else state.get("ExitCode"),
        env_names=sorted({item.partition("=")[0] for item in config.get("Env") or []}),
        mounts=[
            MountFact(destination=str(mount.get("Destination")), read_only=not mount.get("RW"))
            for mount in raw.get("Mounts") or []
        ],
        limits_applied=applied,
        **facts,
    )


def _read_report(project_dir: Path, scenario_id: str, exit_code: int | None, stdout: bytes, runner: ContainerFacts | None) -> Report:
    try:
        payload = json.loads(stdout)
    except (ValueError, RecursionError):
        payload = None
    if not isinstance(payload, dict):
        reason: FailureReason = "runner_oom" if runner is not None and runner.oom_killed else "report_missing"
        raise SandboxError(reason, f"runner exited {exit_code} without a result")
    if payload.get("status") == "error":
        error_class = payload.get("error_class")
        detail = _clean(f"{error_class}: {payload.get('message')}")
        raise SandboxError("target_unreachable" if error_class == "target_unreachable" else "runner_error", detail)
    if payload.get("status") == "NOT_COMPARABLE":
        raise SandboxError("not_comparable", _clean(str(payload.get("not_comparable_reason"))))
    run_id = payload.get("run_id")
    try:
        directory = report_module.run_dir(project_dir, str(run_id))
        path = directory / report_module.REPORT_NAME
        if path.stat().st_size > MAX_REPORT_BYTES:
            raise SandboxError("report_invalid", f"report is larger than {MAX_REPORT_BYTES} bytes")
        report = report_module.read_report(project_dir, str(run_id))
    except (FaultForgeError, OSError) as error:
        raise SandboxError("report_invalid", _clean(str(error))) from error
    if report.scenario_id != scenario_id:
        raise SandboxError("report_invalid", "the report is for a different scenario")
    if EXIT_CODES[report.verdict] != exit_code:
        raise SandboxError("report_invalid", f"runner exited {exit_code} with a {report.verdict} report")
    return report


# One run


@dataclass
class _Session:
    spec: TargetSpec
    project_dir: Path
    scenario_id: str
    options: RunOptions
    policy: SandboxPolicy
    started: float
    started_wall: float = field(default_factory=time.time)
    sandbox_id: str = field(default_factory=lambda: f"sb-{secrets.token_hex(6)}")
    code_dir: Path | None = None
    deployed_code_sha256: str | None = None
    database_state: dict[str, int] | None = None
    database_failure: str | None = None
    containers_started: bool = False
    collected: bool = False
    runner_stdout: bytes = b""
    target_stdout: bytes = b""
    target_log_tail: str = ""
    target_facts: ContainerFacts | None = None
    runner_facts: ContainerFacts | None = None
    containers_left: int | None = None
    service_facts: list[ContainerFacts] = field(default_factory=list)

    @property
    def target_name(self) -> str:
        return f"ff-{self.sandbox_id}-target"

    @property
    def runner_name(self) -> str:
        return f"ff-{self.sandbox_id}-runner"

    def _remaining_s(self) -> float:
        return self.started + self.policy.wall_limit_s - time.monotonic()

    def _within_wall(self, args: list[str], failure: FailureReason) -> subprocess.CompletedProcess:
        timeout_s = min(self.policy.docker_timeout_s, self._remaining_s())
        try:
            result = _docker(self.policy, args, timeout_s)
        except _DockerTimeout as error:
            if self._remaining_s() <= 0:
                raise SandboxError("wall_limit_exceeded", f"{args[0]} ran past the wall limit") from error
            raise
        if result.returncode == 0:
            return result
        detail = _stderr(result)
        if "No such image" in detail or "Unable to find image" in detail:
            raise SandboxError("image_missing", detail)
        if "Cannot connect to the Docker daemon" in detail:
            raise SandboxError("docker_unavailable", detail)
        raise SandboxError(failure, detail)

    def _require_images(self) -> None:
        for image in (self.policy.target_image, self.policy.runner_image):
            result = _docker(self.policy, ["image", "inspect", "--format", "{{.Id}}", image])
            if result.returncode != 0:
                raise SandboxError("image_missing", f"{image} isn't built; run scripts/build_images.sh")

    def _target_is_running(self) -> bool:
        result = _docker(self.policy, ["inspect", "--format", "{{.State.Running}}", self.target_name])
        return result.stdout.strip() == b"true"

    def execute(self) -> Report:
        expires_at = expiry_unix(self.started_wall, self.policy)
        try:
            _require_valid_verifier(self.spec.state_verifier)
            _prepare_project(self.spec, self.project_dir, self.scenario_id)
            runner_args = runner_argv(
                self.runner_name, self.sandbox_id, self.target_name, self.project_dir,
                self.scenario_id, self.options, self.policy, expires_at,
            )
            self.code_dir = write_code_dir(self.spec)
            self.deployed_code_sha256 = code_dir_sha256(self.code_dir)
            target_args = target_argv(
                self.target_name, self.sandbox_id, self.code_dir, self.spec.allow_hostile, self.policy, expires_at,
            )
        except FaultForgeError as error:
            raise SandboxError("invalid_spec", _clean(error.message)) from error
        except (OSError, UnicodeDecodeError) as error:
            raise SandboxError("invalid_spec", _clean(f"{type(error).__name__}: {error}")) from error

        reap_stale(self.policy)
        self._require_images()

        self.containers_started = True
        self._within_wall(target_args, "target_start_failed")
        try:
            self._within_wall(runner_args, "runner_start_failed")
        except SandboxError as error:
            # Joining a stopped container's namespace fails, so the target usually caused this.
            if error.reason == "runner_start_failed" and not self._target_is_running():
                raise SandboxError("target_exited", error.detail) from error
            raise
        return self._await_runner()

    def _await_runner(self) -> Report:
        try:
            waited = _docker(self.policy, ["wait", self.runner_name], self._remaining_s())
        except _DockerTimeout as error:
            self.collect()
            raise SandboxError(
                "wall_limit_exceeded", f"killed at the {self.policy.wall_limit_s:g} s wall limit"
            ) from error
        # Straight after the runner, while the target is up and nothing has touched its state.
        self._read_database()
        self.collect()
        try:
            exit_code = int(waited.stdout.strip())
        except ValueError as error:
            raise SandboxError("docker_unavailable", f"docker wait failed: {_stderr(waited)}") from error
        return _read_report(self.project_dir, self.scenario_id, exit_code, self.runner_stdout, self.runner_facts)

    def _read_database(self) -> None:
        """Doesn't raise: a gap here is a result of unavailable, and never a failed run."""
        queries = self.spec.state_verifier
        if queries is None:
            return
        try:
            self.database_state = self._query_database(queries)
        except _DockerTimeout:
            self.database_failure = "exec_timed_out"
        except SandboxError:
            self.database_failure = "docker_unavailable"
        except ValueError:
            self.database_failure = "invalid_output"
        except Exception as error:
            self.database_failure = f"internal_error_{type(error).__name__}"

    def _query_database(self, queries: Mapping[str, str]) -> dict[str, int] | None:
        if not self._target_is_running():
            self.database_failure = "target_exited"
            return None
        timeout_s = min(self.policy.docker_timeout_s, STATE_EXEC_TIMEOUT_S)
        with tempfile.TemporaryFile() as out:
            result = _docker(
                self.policy, state_verifier_argv(self.target_name, queries), timeout_s,
                stdout=out, stderr=subprocess.DEVNULL,
            )
            out.seek(0)
            output = out.read(MAX_STATE_OUTPUT_BYTES + 1)
        if result.returncode != 0:
            # stderr is left out: it could quote whatever is in the target's database file.
            self.database_failure = f"exec_exited_{result.returncode}"
            return None
        return parse_state_output(output, queries)

    def state_verification(self, report: Report | None) -> StateVerification | None:
        if self.spec.state_verifier is None:
            return None
        if report is None:
            return StateVerification(status="unavailable", reason="no_report")
        if self.database_state is None:
            return StateVerification(status="unavailable", reason=self.database_failure or "not_read")
        return compare_state(self.database_state, self.project_dir, report)

    def _read_logs(self, name: str, stdout_limit: int) -> tuple[bytes, str]:
        """To files first: the log driver bounds their size, but a pipe would hold it all in memory."""
        with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
            _docker(self.policy, ["logs", name], stdout=out, stderr=err)
            size = out.seek(0, os.SEEK_END)
            out.seek(0)
            head = out.read(stdout_limit)
            out.seek(max(0, size - LOG_TAIL_BYTES))
            stdout_tail = out.read()
            err_size = err.seek(0, os.SEEK_END)
            err.seek(max(0, err_size - LOG_TAIL_BYTES))
            stderr_tail = err.read()
        tail = (stderr_tail + b"\n" + stdout_tail).decode("utf-8", "replace")
        return head, _clean(tail, LOG_TAIL_BYTES)

    def _inspect(self, name: str, limits: ContainerLimits, network: str, user: str) -> ContainerFacts | None:
        result = _docker(self.policy, ["inspect", name])
        if result.returncode != 0:
            return None
        items = json.loads(result.stdout)
        return _container_facts(items[0], limits, network, user) if items else None

    def collect(self) -> None:
        """Runs before removal and doesn't raise: a gap here must not stop the cleanup."""
        if self.collected or not self.containers_started:
            return
        self.collected = True
        for step in (self._collect_target, self._collect_runner):
            try:
                step()
            except Exception:
                continue

    def _collect_runner(self) -> None:
        self.runner_stdout, _ = self._read_logs(self.runner_name, MAX_RUNNER_STDOUT_BYTES)
        target_id = self.target_facts.container_id if self.target_facts else ""
        self.runner_facts = self._inspect(
            self.runner_name, self.policy.runner, f"container:{target_id}", f"{os.getuid()}:{os.getgid()}"
        )

    def _collect_target(self) -> None:
        self.target_stdout, self.target_log_tail = self._read_logs(self.target_name, MAX_PROBE_SCAN_BYTES)
        self.target_facts = self._inspect(self.target_name, self.policy.target, "none", TARGET_USER)

    def close(self) -> None:
        """Always removes the containers, and doesn't raise an ordinary exception."""
        try:
            self.collect()
        finally:
            try:
                self.containers_left = _remove_sandbox(self.policy, self.sandbox_id)
            except SandboxError:
                self.containers_left = None
            finally:
                if self.code_dir is not None:
                    shutil.rmtree(self.code_dir, ignore_errors=True)


def run_sandboxed(
    spec: TargetSpec,
    project_dir: Path,
    scenario_id: str,
    options: RunOptions = RunOptions(),
    policy: SandboxPolicy = DEFAULT_POLICY,
) -> SandboxResult:
    """Returns a result for every failure, rather than raising. A failed result has no report."""
    return run_session(_Session(spec, Path(project_dir).resolve(), scenario_id, options, policy, time.monotonic()))


def run_session(session: _Session) -> SandboxResult:
    """Everything after the session's own start-up is shared, so every kind of target ends the same way."""
    policy = session.policy
    report: Report | None = None
    failure: SandboxError | None = None
    try:
        report = session.execute()
    except SandboxError as error:
        failure = error
    except Exception as error:
        failure = SandboxError("internal_error", f"{type(error).__name__}: {error}")
    finally:
        session.close()
    probes = parse_probes(session.target_stdout)
    return SandboxResult(
        status="failed" if failure else "completed",
        failure_reason=failure.reason if failure else None,
        failure_detail=_clean(failure.detail) if failure else None,
        report=report,
        deployed_code_sha256=session.deployed_code_sha256,
        containment=ContainmentFacts(
            target=session.target_facts,
            runner=session.runner_facts,
            containers_left=session.containers_left,
            wall_s=round(time.monotonic() - session.started, 2),
            wall_limit_s=policy.wall_limit_s,
            probes=probes,
            probes_without_result=unfinished_probes(probes),
            target_log_tail=session.target_log_tail,
            state_verification=session.state_verification(report),
            services=session.service_facts,
        ),
    )
