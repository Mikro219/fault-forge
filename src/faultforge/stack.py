"""Runs a multi-service target: an app image, Postgres and, if the stack names one, Redis, sealed together.

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
only as trustworthy as that build, and the app is untrusted here. For a stack with a `build:`
section, node_build.py makes the image from source, and the deployed-code hash is then the host's
hash of that source (StackSpec.code_sha256), because every image build has a new ID.

Three things hold for every stack (docs/PLAN.md M11). Postgres asks for a password on every TCP
login, so the app, which shares its loopback, can't log in as the administrator; the unix socket
inside the postgres container stays open to the host's docker exec. Every admin session pins its
search_path, so a function the app defines in its own schema can't stand in for a built-in one. And
the hooks helper answers only a caller that holds this run's token, which the runner has and the
app doesn't; that stops an app calling reset by accident, and is not a defence against an app that
owns its schema and is written to cheat.
"""

import dataclasses
import hashlib
import json
import os
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

from faultforge import config as config_module
from faultforge.hooks import HOOKS_TOKEN_ENV
from faultforge.lifecycle import RunOptions
from faultforge.models import Contract, Report
from faultforge.node_build import BuildConfig
from faultforge.sandbox import (
    DEFAULT_POLICY,
    MAX_RUNNER_STDOUT_BYTES,
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
# TCP logins need the password. The unix socket keeps initdb's default, trust, and only something
# inside the postgres container can reach it.
POSTGRES_INITDB_ARGS = "--auth-host=scram-sha-256"
# Built-in names resolve to pg_catalog first, whatever the app defines in its own schema.
ADMIN_SEARCH_PATH = "pg_catalog, public"


def admin_conninfo(stack: "StackConfig") -> str:
    """psql's -d value for an admin session: the database, and the search_path as a connection
    option, which outranks a default the app sets on its own database or role."""
    return f"dbname={stack.database_name} options=-csearch_path={ADMIN_SEARCH_PATH.replace(' ', '')}"

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
    redis_image: str | None = None
    # The prebuilt app image the MCP server runs for this stack. The CLI and tests pass their own.
    app_image: str | None = None
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
    # How to build the app from source (node_build.py). Without it the stack runs a prebuilt image only.
    build: BuildConfig | None = None


_PORT_BOUND = "import socket; socket.create_connection(('127.0.0.1', {port}), 1).close()"
# Run inside the helper's container: a call without the run's token must get 403. A stale helper
# image, built before the token existed, answers it, and the run is refused instead of trusted.
_TOKEN_DEMANDED = (
    "import sys, urllib.request, urllib.error\n"
    "try:\n"
    "    urllib.request.urlopen('http://127.0.0.1:{port}/__ff/state', timeout=3)\n"
    "except urllib.error.HTTPError as error:\n"
    "    sys.exit(0 if error.code == 403 else 3)\n"
    "except Exception:\n"
    "    sys.exit(4)\n"
    "sys.exit(5)\n"
)


def load_stack(path: Path) -> StackConfig:
    return StackConfig.model_validate(yaml.safe_load(Path(path).read_text(encoding="utf-8")))


@dataclass(frozen=True)
class StackSpec:
    app_image: str
    stack: StackConfig
    config_path: Path
    # For an image FaultForge built from source: the host's hash of that source. When set, it is the
    # run's deployed_code_sha256; otherwise the image's ID is hashed, as for any prebuilt image.
    code_sha256: str | None = None

    @property
    def state_verifier(self) -> Mapping[str, str]:
        return self.stack.state_sql

    def deployed_code(self, image_id: str) -> str:
        """What the host knows ran: the hash of the source it built, or of a prebuilt image's ID."""
        return self.code_sha256 or hashlib.sha256(image_id.encode()).hexdigest()


def psql_state_argv(container: str, stack: StackConfig, queries: Mapping[str, str]) -> list[str]:
    """One statement, so the values are read together. Names were checked as plain lowercase names."""
    fields = ", ".join(f"'{name}', ({sql})" for name, sql in queries.items())
    return [
        "exec", container, "psql", "-X", "-U", ADMIN_ROLE, "-d", admin_conninfo(stack),
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
    hooks_token: str = ""
    namespace: str = ""

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
            *_env_args({
                "POSTGRES_PASSWORD": admin_password, "POSTGRES_USER": ADMIN_ROLE, "POSTGRES_DB": stack.database_name,
                "POSTGRES_INITDB_ARGS": POSTGRES_INITDB_ARGS,
            }),
            stack.postgres_image,
        ]
        uid = stack.app_user.split(":")[0]
        app = [
            *_common_args(self.target_name, self.sandbox_id, "target", self.policy.target, expires_at),
            *namespace, "--user", stack.app_user,
            *[argument for path in stack.app_writable for argument in ("--tmpfs", f"{path}:rw,uid={uid},size=64m")],
            *_env_args({
                **stack.env, "POSTGRES_HOST": "127.0.0.1", "POSTGRES_USER": stack.database_user, "DATABASE_URL": database_url,
                **({"REDIS_URL": "redis://127.0.0.1:6379"} if stack.redis_image is not None else {}),
                "HOME": "/tmp", "PORT": str(stack.app_port), "HOSTNAME": "127.0.0.1",
            }),
            self.spec.app_image,
        ]
        hooks_spec = {
            "port": stack.hooks_port, "database_user": ADMIN_ROLE, "database_name": stack.database_name,
            "app_health_url": f"http://127.0.0.1:{stack.app_port}{stack.app_health_path}",
            "reset_sql": stack.reset_sql, "state_sql": dict(stack.state_sql),
            "build_id": self.deployed_code_sha256, "instance_id": self.instance_id,
            "started_at": datetime.fromtimestamp(self.started_wall, UTC).isoformat(),
            "search_path": ADMIN_SEARCH_PATH,
        }
        hooks = [
            *_common_args(self.hooks_name, self.sandbox_id, "hooks", HOOKS_LIMITS, expires_at),
            *namespace, "--user", HOOKS_USER,
            *_env_args({
                "FF_STACK_HOOKS": json.dumps(hooks_spec), "FF_DATABASE_PASSWORD": admin_password,
                HOOKS_TOKEN_ENV: self.hooks_token, "PYTHONDONTWRITEBYTECODE": "1",
            }),
            HOOKS_IMAGE, "python", "-I", "/opt/stack_hooks.py",
        ]
        built = {"postgres": postgres, "app": app, "hooks": hooks}
        if stack.redis_image is not None:
            built["redis"] = [
                *_common_args(self.redis_name, self.sandbox_id, "redis", REDIS_LIMITS, expires_at),
                *namespace, "--user", REDIS_USER, "--tmpfs", "/data:rw,uid=999,size=4m",
                stack.redis_image, "redis-server", "--save", "", "--appendonly", "no", "--bind", "127.0.0.1",
            ]
        return built

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
        self._require_helper_origin()
        reap_stale(self.policy)
        for image in (stack.postgres_image, stack.redis_image, HOOKS_IMAGE, self.policy.runner_image):
            if image is not None:
                self._image_id(image)
        # What the host knows ran: the image's own ID, which the app can't change, or for an image
        # FaultForge built from source, the hash of that source.
        self.deployed_code_sha256 = self.spec.deployed_code(self._image_id(self.spec.app_image))
        self.instance_id = secrets.token_hex(8)
        self.hooks_token = secrets.token_hex(16)
        password = secrets.token_hex(16)
        argv = self.argv(password, expires_at, secrets.token_hex(16))
        runner_args = runner_argv(
            self.runner_name, self.sandbox_id, self.postgres_name, self.project_dir, self.scenario_id,
            self.options, self.policy, expires_at, extra_env={HOOKS_TOKEN_ENV: self.hooks_token},
        )
        self.containers_started = True
        self._start(argv["postgres"])
        self._await(["exec", self.postgres_name, "pg_isready", "-h", "127.0.0.1", "-U", ADMIN_ROLE], "postgres")
        setup = _docker(self.policy, [
            "exec", self.postgres_name, "psql", "-X", "-U", ADMIN_ROLE, "-d", admin_conninfo(stack), "-v", "ON_ERROR_STOP=1",
            "-c", setup_role_sql(stack, password),
        ], STATE_EXEC_TIMEOUT_S)
        if setup.returncode != 0:
            raise SandboxError("target_start_failed", "the database role couldn't be set up")
        if "redis" in argv:
            self._start(argv["redis"])
        # Before the app, so that the app can't bind the helper's port first.
        self._start(argv["hooks"])
        self._await(["exec", self.hooks_name, "python", "-I", "-c", _PORT_BOUND.format(port=stack.hooks_port)], "hooks")
        demanded = _docker(
            self.policy, ["exec", self.hooks_name, "python", "-I", "-c", _TOKEN_DEMANDED.format(port=stack.hooks_port)],
            STATE_EXEC_TIMEOUT_S,
        )
        if demanded.returncode != 0:
            raise SandboxError(
                "image_missing", f"{HOOKS_IMAGE} doesn't demand the run's token; rebuild it with scripts/build_images.sh",
            )
        self._start(argv["app"])
        try:
            self._within_wall(runner_args, "runner_start_failed")
        except SandboxError as error:
            if error.reason == "runner_start_failed" and not self._target_is_running():
                raise SandboxError("target_exited", error.detail) from error
            raise
        return self._await_runner()

    def _require_helper_origin(self) -> None:
        """The runner sends this run's hooks token to target.hooks_origin, so that must be the
        helper's own port and never the app's."""
        config = config_module.parse_config(Path(self.spec.config_path).read_text(encoding="utf-8"))
        expected = f"http://127.0.0.1:{self.spec.stack.hooks_port}"
        if config.target.hooks_origin != expected:
            raise SandboxError("invalid_spec", f"a stack's target.hooks_origin must be {expected}, where the hooks helper listens")

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
        self.namespace = joined
        self.target_facts = self._inspect(self.target_name, self.policy.target, joined, self.spec.stack.app_user)
        others = [(self.hooks_name, HOOKS_LIMITS, HOOKS_USER)]
        if self.spec.stack.redis_image is not None:
            others.insert(0, (self.redis_name, REDIS_LIMITS, REDIS_USER))
        for name, limits, user in others:
            facts = self._inspect(name, limits, joined, user)
            if facts is not None:
                self.service_facts.append(facts)


    def _collect_runner(self) -> None:
        """A stack's runner joins postgres's namespace, not the app's, so that is what its facts are
        checked against."""
        self.runner_stdout, _ = self._read_logs(self.runner_name, MAX_RUNNER_STDOUT_BYTES)
        self.runner_facts = self._inspect(self.runner_name, self.policy.runner, self.namespace or "unknown", f"{os.getuid()}:{os.getgid()}")


def run_stack(
    spec: StackSpec, project_dir: Path, scenario_id: str, options: RunOptions = RunOptions(),
    policy: SandboxPolicy = STACK_POLICY,
) -> SandboxResult:
    """Returns a result for every failure, rather than raising. A failed result has no report."""
    return run_session(StackSession(spec, Path(project_dir).resolve(), scenario_id, options, policy, time.monotonic()))
