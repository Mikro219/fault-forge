"""Runs a multi-service target: a pre-built app image, Postgres and Redis, sealed together.

Postgres owns a network namespace with no network, and everything else joins it, so 127.0.0.1 reaches
the other services and nothing else exists. Every container has the same limits as a Python target's.
Secrets are random for each run, or fixed test values in the stack's definition. Nothing is read
from a repo's own env files. The deployed-code hash is the image's ID, and the host reads the database
with psql as an administrator the app can't log in as, and the hooks helper starts before the app, so
it can't take the helper's port. A run where a trusted service died can't pass the gate.

The app owns its own database, so like any Python target it can still edit its data, including
replacing a table with a view. Client counts are measured by the runner, but a cheating app could
also keep its state in memory. So a verified fix is strong evidence against honest bugs, and not
proof against code written to fool the check.

The image is built beforehand, because a real build doesn't fit in a sandbox's wall limit. It's
only as trustworthy as that build, and the app is untrusted here.
"""

import dataclasses
import hashlib
import json
import secrets
import subprocess
import tempfile
import time
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import yaml
from pydantic import Field

from faultforge.lifecycle import RunOptions
from faultforge.models import Contract, Report
from faultforge.sandbox import (
    DEFAULT_POLICY,
    MAX_STATE_OUTPUT_BYTES,
    STATE_EXEC_TIMEOUT_S,
    ContainerLimits,
    SandboxError,
    SandboxPolicy,
    SandboxResult,
    StateVerification,
    _common_args,
    _docker,
    _env_args,
    _prepare_project,
    _Session,
    _require_valid_verifier,
    expiry_unix,
    parse_state_output,
    reap_stale,
    run_session,
    runner_argv,
)

HOOKS_IMAGE = "faultforge-stack-hooks:dev"
POSTGRES_USER = "70:70"
REDIS_USER = "999:1000"
HOOKS_USER = "65534:65534"
ADMIN_ROLE = "faultforge_admin"
READY_WAIT_S = 30.0
READY_POLL_S = 0.25

# Measured in the SportsDeck spike: 114 MiB, 108 MiB and 15 MiB after load. Start-up runs migrations,
# so the app's limit leaves room for those too.
STACK_POLICY = dataclasses.replace(
    DEFAULT_POLICY,
    target=ContainerLimits(memory_mb=1024, cpus=1.0, pids=256, tmpfs_mb=64, log_mb=4),
    wall_limit_s=150.0,
    wait_for_target_s=30.0,
)
POSTGRES_LIMITS = ContainerLimits(memory_mb=512, cpus=1.0, pids=128, tmpfs_mb=16, log_mb=1)
REDIS_LIMITS = ContainerLimits(memory_mb=128, cpus=0.5, pids=32, tmpfs_mb=8, log_mb=1)
HOOKS_LIMITS = ContainerLimits(memory_mb=128, cpus=0.5, pids=32, tmpfs_mb=8, log_mb=1)


class StackConfig(Contract):
    """A stack's definition. It's trusted: it comes from this repo, never from a request."""

    postgres_image: str
    redis_image: str
    database_user: str = Field(pattern=r"^[a-z][a-z0-9_]{0,30}$")
    database_name: str = Field(pattern=r"^[a-z][a-z0-9_]{0,30}$")
    app_port: int = 8000
    app_user: str = Field(pattern=r"^[0-9]+:[0-9]+$")
    app_health_path: str
    app_writable: list[str] = Field(default_factory=list)
    hooks_port: int = 8001
    env: dict[str, str] = Field(default_factory=dict, description="Fixed test values only, never real secrets")
    reset_sql: list[str] = Field(min_length=1)
    state_sql: dict[str, str] = Field(min_length=1)


_PORT_BOUND = "import socket; socket.create_connection(('127.0.0.1', {port}), 1).close()"


def load_stack(path: Path) -> StackConfig:
    return StackConfig.model_validate(yaml.safe_load(Path(path).read_text(encoding="utf-8")))


@dataclass(frozen=True)
class StackSpec:
    app_image: str
    stack: StackConfig
    config_path: Path

    @property
    def state_verifier(self) -> Mapping[str, str]:
        return self.stack.state_sql


def psql_state_argv(container: str, stack: StackConfig, queries: Mapping[str, str]) -> list[str]:
    """One statement, so the values are read together. Names were checked as plain lowercase names."""
    fields = ", ".join(f"'{name}', ({sql})" for name, sql in queries.items())
    return [
        "exec", container, "psql", "-X", "-U", ADMIN_ROLE, "-d", stack.database_name,
        "-tA", "-v", "ON_ERROR_STOP=1", "-c", f"SELECT json_build_object({fields})",
    ]


def setup_role_sql(stack: StackConfig, password: str) -> str:
    """The app gets an ordinary role that owns only its own database, so it has no superuser powers
    such as COPY FROM PROGRAM. The names were checked as plain lowercase, and the password is hex."""
    role, database = stack.database_user, stack.database_name
    return (
        f"CREATE ROLE {role} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE PASSWORD '{password}'; "
        f"ALTER DATABASE {database} OWNER TO {role}; "
        f"ALTER SCHEMA public OWNER TO {role};"
    )


@dataclass
class StackSession(_Session):
    instance_id: str = ""

    @property
    def postgres_name(self) -> str:
        return f"ff-{self.sandbox_id}-postgres"

    @property
    def redis_name(self) -> str:
        return f"ff-{self.sandbox_id}-redis"

    @property
    def hooks_name(self) -> str:
        return f"ff-{self.sandbox_id}-hooks"

    def _start(self, args: list[str], failure: str = "target_start_failed") -> None:
        self._within_wall(args, failure)  # type: ignore[arg-type]

    def _image_id(self, image: str) -> str:
        result = _docker(self.policy, ["image", "inspect", "--format", "{{.Id}}", image])
        if result.returncode != 0:
            raise SandboxError("image_missing", f"{image} isn't built")
        return result.stdout.decode("ascii", "replace").strip()

    def argv(self, password: str, expires_at: int, admin_password: str = "") -> dict[str, list[str]]:
        stack = self.spec.stack
        namespace = ["--network", f"container:{self.postgres_name}"]
        database_url = f"postgresql://{stack.database_user}:{password}@127.0.0.1:5432/{stack.database_name}"
        postgres = [
            *_common_args(self.postgres_name, self.sandbox_id, "postgres", POSTGRES_LIMITS, expires_at),
            "--network", "none", "--user", POSTGRES_USER,
            "--tmpfs", "/var/lib/postgresql/data:rw,uid=70,gid=70,mode=0700,size=256m",
            "--tmpfs", "/var/run/postgresql:rw,uid=70,gid=70,size=1m",
            *_env_args({"POSTGRES_PASSWORD": admin_password, "POSTGRES_USER": ADMIN_ROLE, "POSTGRES_DB": stack.database_name}),
            stack.postgres_image,
        ]
        redis = [
            *_common_args(self.redis_name, self.sandbox_id, "redis", REDIS_LIMITS, expires_at),
            *namespace, "--user", REDIS_USER, "--tmpfs", "/data:rw,uid=999,size=4m",
            stack.redis_image, "redis-server", "--save", "", "--appendonly", "no", "--bind", "127.0.0.1",
        ]
        uid = stack.app_user.split(":")[0]
        app = [
            *_common_args(self.target_name, self.sandbox_id, "target", self.policy.target, expires_at),
            *namespace, "--user", stack.app_user,
            *[argument for path in stack.app_writable for argument in ("--tmpfs", f"{path}:rw,uid={uid},size=64m")],
            *_env_args({
                **stack.env, "POSTGRES_HOST": "127.0.0.1", "POSTGRES_USER": stack.database_user, "DATABASE_URL": database_url,
                "REDIS_URL": "redis://127.0.0.1:6379", "HOME": "/tmp", "PORT": str(stack.app_port), "HOSTNAME": "127.0.0.1",
            }),
            self.spec.app_image,
        ]
        hooks_spec = {
            "port": stack.hooks_port, "database_user": ADMIN_ROLE, "database_name": stack.database_name,
            "app_health_url": f"http://127.0.0.1:{stack.app_port}{stack.app_health_path}",
            "reset_sql": stack.reset_sql, "state_sql": dict(stack.state_sql),
            "build_id": self.deployed_code_sha256, "instance_id": self.instance_id,
            "started_at": datetime.fromtimestamp(self.started_wall, UTC).isoformat(),
        }
        hooks = [
            *_common_args(self.hooks_name, self.sandbox_id, "hooks", HOOKS_LIMITS, expires_at),
            *namespace, "--user", HOOKS_USER,
            *_env_args({"FF_STACK_HOOKS": json.dumps(hooks_spec), "FF_DATABASE_PASSWORD": admin_password, "PYTHONDONTWRITEBYTECODE": "1"}),
            HOOKS_IMAGE, "python", "-I", "/opt/stack_hooks.py",
        ]
        return {"postgres": postgres, "redis": redis, "app": app, "hooks": hooks}

    def execute(self) -> Report:
        stack = self.spec.stack
        expires_at = expiry_unix(self.started_wall, self.policy)
        _require_valid_verifier(self.spec.state_verifier)
        try:
            _prepare_project(self.spec, self.project_dir, self.scenario_id)
        except Exception as error:
            if isinstance(error, SandboxError):
                raise
            raise SandboxError("invalid_spec", type(error).__name__) from error
        reap_stale(self.policy)
        for image in (stack.postgres_image, stack.redis_image, HOOKS_IMAGE, self.policy.runner_image):
            self._image_id(image)
        # What the host knows ran: the image's own ID, which the app can't change.
        self.deployed_code_sha256 = hashlib.sha256(self._image_id(self.spec.app_image).encode()).hexdigest()
        self.instance_id = secrets.token_hex(8)
        password = secrets.token_hex(16)
        argv = self.argv(password, expires_at, secrets.token_hex(16))
        runner_args = runner_argv(
            self.runner_name, self.sandbox_id, self.postgres_name, self.project_dir, self.scenario_id,
            self.options, self.policy, expires_at,
        )
        self.containers_started = True
        self._start(argv["postgres"])
        self._await(["exec", self.postgres_name, "pg_isready", "-h", "127.0.0.1", "-U", ADMIN_ROLE], "postgres")
        setup = _docker(self.policy, [
            "exec", self.postgres_name, "psql", "-X", "-U", ADMIN_ROLE, "-d", stack.database_name, "-v", "ON_ERROR_STOP=1",
            "-c", setup_role_sql(stack, password),
        ], STATE_EXEC_TIMEOUT_S)
        if setup.returncode != 0:
            raise SandboxError("target_start_failed", "the database role couldn't be set up")
        self._start(argv["redis"])
        # Before the app, so that the app can't bind the helper's port first.
        self._start(argv["hooks"])
        self._await(["exec", self.hooks_name, "python", "-I", "-c", _PORT_BOUND.format(port=stack.hooks_port)], "hooks")
        self._start(argv["app"])
        try:
            self._within_wall(runner_args, "runner_start_failed")
        except SandboxError as error:
            if error.reason == "runner_start_failed" and not self._target_is_running():
                raise SandboxError("target_exited", error.detail) from error
            raise
        return self._await_runner()

    def _await(self, args: list[str], name: str) -> None:
        deadline = time.monotonic() + READY_WAIT_S
        while time.monotonic() < deadline:
            if _docker(self.policy, args, STATE_EXEC_TIMEOUT_S).returncode == 0:
                return
            time.sleep(READY_POLL_S)
        raise SandboxError("target_start_failed", f"{name} didn't become ready")

    def state_verification(self, report: Report | None) -> StateVerification | None:
        """A run where a trusted service died can't pass, because the hooks' answers mean nothing."""
        names = {POSTGRES_USER, HOOKS_USER}
        trusted = [facts for facts in self.service_facts if facts.user in names]
        if len(trusted) != len(names) or not all(facts.running_when_inspected for facts in trusted):
            return StateVerification(status="unavailable", reason="trusted_service_not_running")
        return super().state_verification(report)

    def _query_database(self, queries: Mapping[str, str]) -> dict[str, int] | None:
        timeout_s = min(self.policy.docker_timeout_s, STATE_EXEC_TIMEOUT_S)
        with tempfile.TemporaryFile() as out:
            result = _docker(
                self.policy, psql_state_argv(self.postgres_name, self.spec.stack, queries), timeout_s,
                stdout=out, stderr=subprocess.DEVNULL,
            )
            out.seek(0)
            output = out.read(MAX_STATE_OUTPUT_BYTES + 1)
        if result.returncode != 0:
            self.database_failure = f"exec_exited_{result.returncode}"
            return None
        return parse_state_output(output, queries)

    def _collect_target(self) -> None:
        self.target_stdout, self.target_log_tail = self._read_logs(self.target_name, 1024 * 1024)
        postgres = self._inspect(self.postgres_name, POSTGRES_LIMITS, "none", POSTGRES_USER)
        if postgres is not None:
            self.service_facts.append(postgres)
        joined = f"container:{postgres.container_id}" if postgres is not None else ""
        self.target_facts = self._inspect(self.target_name, self.policy.target, joined, self.spec.stack.app_user)
        for name, limits, user in ((self.redis_name, REDIS_LIMITS, REDIS_USER), (self.hooks_name, HOOKS_LIMITS, HOOKS_USER)):
            facts = self._inspect(name, limits, joined, user)
            if facts is not None:
                self.service_facts.append(facts)


def run_stack(
    spec: StackSpec, project_dir: Path, scenario_id: str, options: RunOptions = RunOptions(),
    policy: SandboxPolicy = STACK_POLICY,
) -> SandboxResult:
    """Returns a result for every failure, rather than raising. A failed result has no report."""
    return run_session(StackSession(spec, Path(project_dir).resolve(), scenario_id, options, policy, time.monotonic()))
