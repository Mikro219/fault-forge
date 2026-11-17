"""M11: the redeem-code demo in real containers.

The default tests need the images scripts/build_redeem_demo.sh makes and skip without them. They
run the prebuilt buggy and fixed images, and check what M11 changed for every stack: postgres asks
for a password on every TCP login, trusted sessions pin their search_path, and there is no redis.
The pinned SQL runs as faultforge_hooks, an ordinary role: the tests fire a trigger on reset and
check it can't run a program, force a row-security policy and check the state read fails rather
than being filtered, and read the catalog inventory a fix is later compared on.

The demo's migrations are a prestart step (tools/migrate.ts as redeem_owner, before the app, which
logs in as redeem_app): the default tests check the roles, the step's containment and its failure.

FF_TEST_STACK_BUILD=1 adds the real builder through the MCP tools: a baseline from source, both
reference fixes (one in src/, one a migration), every false fix and a hostile build. They need a warm install cache and skip on a
cold one. FF_TEST_NPM_NETWORK=1 adds the one test that uses the network: a cold fetch of the demo's
lockfile from registry.npmjs.org through the proxy. Run it first to warm the cache for the others.
"""

import dataclasses
import json
import os
import re
import secrets
import shutil
import subprocess
import time
from pathlib import Path

import pytest

from faultforge import fix_agent, node_build, sandbox
from faultforge.lifecycle import RunOptions
from faultforge.mcp_server import credit, settings as settings_module
from faultforge.mcp_server.tools import Tools
from faultforge.sandbox import _common_args, _docker, _remove_sandbox
from faultforge import stack as stack_module, stack_hooks
from faultforge.stack import (
    ADMIN_ROLE, HOOKS_IMAGE, HOOKS_ROLE, STACK_POLICY, StackSession, StackSpec, admin_psql_argv, load_stack, psql_state_argv, run_stack,
    setup_role_sql,
)

ROOT = Path(__file__).resolve().parents[1]
DEMO = ROOT / "demo" / "redeem-code"
SPORTSDECK = ROOT / "demo" / "sportsdeck"
CONFIG = DEMO / "faultforge.yaml"
STACK = load_stack(DEMO / "stack.yaml")
BUGGY, FIXED = "faultforge-redeem-code:buggy", "faultforge-redeem-code:fixed"
FIXED_SCHEMA = "faultforge-redeem-code:fixed-schema"
OWNER = STACK.prestart.owner_role
RACE = "same-code-race"
REGRESSION = ["already-redeemed-refused", "revoked-code-refused", "unknown-code-refused"]
# Kept between runs, so the install cache it holds stays warm. .faultforge/ is ignored by git.
TEST_STATE = ROOT / ".faultforge" / "m11-test-state"
GATES = [fix_agent.GATE_FIX_VERIFIED, fix_agent.GATE_STATE, fix_agent.GATE_CODE, fix_agent.GATE_REGRESSION]


def has_image(image: str) -> bool:
    if shutil.which("docker") is None:
        return False
    return subprocess.run(["docker", "image", "inspect", image], capture_output=True, check=False).returncode == 0


demo_images = pytest.mark.skipif(
    not all(has_image(image) for image in (BUGGY, FIXED, FIXED_SCHEMA, HOOKS_IMAGE, STACK.postgres_image)),
    reason="run scripts/build_redeem_demo.sh first",
)
postgres_images = pytest.mark.skipif(
    not all(has_image(image) for image in (HOOKS_IMAGE, STACK.postgres_image)), reason="run scripts/build_images.sh first",
)
real_build = pytest.mark.skipif(
    not (os.environ.get("FF_TEST_STACK_BUILD") == "1" and has_image(STACK.build.build_image) and has_image(STACK.build.run_image)),
    reason="set FF_TEST_STACK_BUILD=1 and build the toolchain images: real builds take minutes",
)
npm_network = pytest.mark.skipif(
    not (os.environ.get("FF_TEST_NPM_NETWORK") == "1" and has_image(STACK.build.build_image) and has_image(node_build.PROXY_IMAGE)),
    reason="set FF_TEST_NPM_NETWORK=1: this test fetches from registry.npmjs.org",
)


def left(result) -> int | None:
    return result.containment.containers_left


# The prebuilt images


@demo_images
def test_the_buggy_image_redeems_one_code_many_times(tmp_path: Path) -> None:
    result = run_stack(StackSpec(BUGGY, STACK, CONFIG), tmp_path / "project", RACE)
    assert result.status == "completed", (result.failure_reason, result.failure_detail)
    report = result.report
    assert report.verdict == "FAIL"
    concurrent = [trial for trial in report.trials if trial.kind == "concurrent"]
    serial = [trial for trial in report.trials if trial.kind == "serial_control"]
    # Never an exact count: how many win a race isn't fixed. One trial with more than one is the bug.
    assert any(trial.client.accepted > 1 for trial in concurrent)
    assert serial and all(trial.client.accepted == 1 for trial in serial)
    assert all(trial.client.unexpected == 0 and trial.client.transport_error == 0 for trial in report.trials)
    assert "A code is redeemed at most once" in {violation.invariant for violation in report.violations}
    facts = result.containment
    assert facts.state_verification.status == "matched" and left(result) == 0
    # No redis: postgres and the hooks helper are the only trusted services. The third is the prestart,
    # read before it was removed: the app's user and limits, in postgres's namespace, exit status 0.
    assert sorted(service.user for service in facts.services) == ["1000:1000", "65534:65534", "70:70"]
    prestart = next(service for service in facts.services if service.user == "1000:1000")
    assert prestart.limits_applied and prestart.exit_code == 0 and not prestart.running_when_inspected and not prestart.oom_killed
    assert prestart.network_mode == facts.target.network_mode and prestart.read_only_rootfs and prestart.cap_drop == ["ALL"]
    assert prestart.memory_bytes == facts.target.memory_bytes and prestart.pids_limit == facts.target.pids_limit
    assert prestart.mounts == [] or all(mount.destination == "/tmp" for mount in prestart.mounts)
    # It alone held the owner's URL, and never the token or the administrator's password.
    assert {"MIGRATION_DATABASE_URL", "DATABASE_URL"} <= set(prestart.env_names)
    for name in ("FF_HOOKS_TOKEN", "FF_DATABASE_PASSWORD", "FF_STACK_HOOKS", "POSTGRES_PASSWORD"):
        assert name not in prestart.env_names
    assert "MIGRATION_DATABASE_URL" not in facts.target.env_names and "MIGRATION_DATABASE_URL" not in facts.runner.env_names
    assert facts.target.user == "1000:1000" and facts.target.limits_applied and all(service.limits_applied for service in facts.services)
    assert facts.target.network_mode.startswith("container:") and "FF_HOOKS_TOKEN" not in facts.target.env_names
    # The runner joins postgres's namespace too, and it alone holds the hooks token.
    assert facts.runner.limits_applied and facts.runner.network_mode == facts.target.network_mode
    assert "FF_HOOKS_TOKEN" in facts.runner.env_names
    assert "REDIS_URL" not in facts.target.env_names and "POSTGRES_PASSWORD" not in facts.target.env_names
    # The helper holds a database password and the token, as names only here; the app holds neither.
    helper = next(service for service in facts.services if service.user == "65534:65534")
    assert {"FF_DATABASE_PASSWORD", "FF_HOOKS_TOKEN", "FF_STACK_HOOKS"} <= set(helper.env_names)
    assert "FF_DATABASE_PASSWORD" not in facts.target.env_names
    # The catalog inventory was read after the prestart: the demo's schema has nothing that can change a read.
    assert facts.schema_objects == [] and facts.schema_objects_reason is None and facts.schema_objects_during == []


@demo_images
def test_the_fixed_image_passes_the_gate_and_every_regression_scenario(tmp_path: Path) -> None:
    project = tmp_path / "project"
    buggy = run_stack(StackSpec(BUGGY, STACK, CONFIG), project, RACE)
    assert buggy.status == "completed" and buggy.report.verdict == "FAIL"
    spec = StackSpec(FIXED, STACK, CONFIG)
    fixed = run_stack(spec, project, RACE, RunOptions(compare_to=buggy.report.run_id))
    assert fixed.status == "completed", (fixed.failure_reason, fixed.failure_detail)
    assert fixed.report.verdict == "PASS" and fixed.report.comparison.fix_verified is True
    assert fixed.containment.state_verification.status == "matched"
    assert fix_agent.gate_failures(fixed, buggy.deployed_code_sha256) == ()
    regression = fix_agent.run_regression(spec, project, REGRESSION, RunOptions(), run_stack, fixed.deployed_code_sha256, CONFIG)
    assert [(item.scenario_id, item.passed, item.verdict, item.state_verification) for item in regression] == [
        (name, True, "PASS", "matched") for name in REGRESSION
    ]
    assert left(buggy) == 0 and left(fixed) == 0


@demo_images
def test_the_schema_fix_passes_the_gate_through_a_migration_the_prestart_applied(tmp_path: Path) -> None:
    """The fix is a unique constraint in migrations/0001, which only the prestart can apply: the app
    role can't alter a table. Without the step the constraint wouldn't exist and the race would remain."""
    project = tmp_path / "project"
    buggy = run_stack(StackSpec(BUGGY, STACK, CONFIG), project, RACE)
    assert buggy.status == "completed" and buggy.report.verdict == "FAIL"
    spec = StackSpec(FIXED_SCHEMA, STACK, CONFIG)
    fixed = run_stack(spec, project, RACE, RunOptions(compare_to=buggy.report.run_id))
    assert fixed.status == "completed", (fixed.failure_reason, fixed.failure_detail)
    assert fixed.report.verdict == "PASS" and fixed.report.comparison.fix_verified is True
    assert fixed.containment.state_verification.status == "matched"
    assert fix_agent.gate_failures(fixed, buggy.deployed_code_sha256) == ()
    assert all(trial.client.unexpected == 0 for trial in fixed.report.trials), "a loser gets 409, never a 500"
    # A unique constraint is none of the objects the inventory lists, so the fix's is the baseline's.
    assert fixed.containment.schema_objects == buggy.containment.schema_objects == []
    regression = fix_agent.run_regression(spec, project, REGRESSION, RunOptions(), run_stack, fixed.deployed_code_sha256, CONFIG)
    assert [(item.scenario_id, item.passed, item.verdict, item.state_verification) for item in regression] == [
        (name, True, "PASS", "matched") for name in REGRESSION
    ]
    assert left(buggy) == 0 and left(fixed) == 0


@demo_images
@pytest.mark.parametrize(("command", "said"), [
    (["node", "tools/missing.ts"], "MODULE_NOT_FOUND"),      # node can't find the script: exit status 1
    (["node", "--no-such-flag"], "bad option"),              # node refuses its arguments: exit status 9
])
def test_a_prestart_that_fails_stops_the_real_stack_and_leaves_nothing(tmp_path: Path, command: list[str], said: str) -> None:
    stack = STACK.model_copy(update={"prestart": STACK.prestart.model_copy(update={"command": command})})
    result = run_stack(StackSpec(BUGGY, stack, CONFIG), tmp_path / "project", RACE)
    assert result.status == "failed" and result.report is None and result.failure_reason == "target_start_failed", result
    assert re.match(r"the prestart step exited with status [1-9][0-9]*\. Its output: \[project\] ", result.failure_detail)
    assert said in result.failure_detail and left(result) == 0
    # The app, the helper and the runner never started.
    assert result.containment.target is None and result.containment.runner is None
    assert sorted(service.user for service in result.containment.services) == ["1000:1000", "70:70"]


@demo_images
def test_a_migration_runner_that_refuses_its_settings_stops_the_stack(tmp_path: Path) -> None:
    """The demo's runner grants to redeem_app by name, so it refuses an app URL for any other role
    before it applies anything. Its own one-line reason comes back marked as the project's text."""
    stack = STACK.model_copy(update={"database_user": "redeem_other"})
    result = run_stack(StackSpec(BUGGY, stack, CONFIG), tmp_path / "project", RACE)
    assert result.status == "failed" and result.failure_reason == "target_start_failed" and result.report is None
    assert "[project] migrate: DATABASE_URL must be for role redeem_app" in result.failure_detail and left(result) == 0


# What M11 changed for every stack


PROBE = """
import json, sys
import pg8000.native
results = []
for case in json.loads(sys.argv[1]):
    try:
        connection = pg8000.native.Connection(case["user"], password=case.get("password"), host=case["host"], database=case["database"], timeout=5)
        rows = None
        for statement in case.get("sql", ["SELECT 1"]):
            rows = connection.run(statement)
        connection.close()
        results.append({"ok": True, "rows": rows})
    except Exception as error:
        detail = error.args[0] if error.args and isinstance(error.args[0], dict) else {}
        results.append({"ok": False, "error": type(error).__name__, "sqlstate": detail.get("C")})
print(json.dumps(results))
"""


class Postgres:
    """A stack's postgres alone, started exactly as StackSession starts it, with the app's role set up."""

    def __init__(self, stack, config: Path, tmp_path: Path):
        self.stack = stack
        self.session = StackSession(StackSpec("unused", stack, config), tmp_path, "unused", RunOptions(), STACK_POLICY, time.monotonic())
        self.app_password, self.admin_password = secrets.token_hex(16), secrets.token_hex(16)
        self.owner_password = secrets.token_hex(16) if stack.prestart is not None else ""
        self.hooks_password = secrets.token_hex(16)
        self.expires = int(time.time()) + 300

    def __enter__(self) -> "Postgres":
        session = self.session
        argv = session.argv(self.app_password, self.expires, self.admin_password, self.owner_password, self.hooks_password)
        try:
            assert _docker(STACK_POLICY, argv["postgres"]).returncode == 0
            session._await(["exec", session.postgres_name, "pg_isready", "-h", "127.0.0.1", "-U", ADMIN_ROLE], "postgres")
            setup = self.psql(setup_role_sql(self.stack, self.app_password, self.owner_password, hooks_password=self.hooks_password))
            assert setup.returncode == 0, setup.stderr
        except BaseException:
            _remove_sandbox(STACK_POLICY, session.sandbox_id)
            raise
        return self

    def __exit__(self, *_: object) -> None:
        assert _remove_sandbox(STACK_POLICY, self.session.sandbox_id) == 0

    def psql(self, sql: str) -> subprocess.CompletedProcess:
        """As the administrator, the way the sandbox sets the roles up: docker exec into the postgres
        container, over its unix socket."""
        return _docker(STACK_POLICY, [*admin_psql_argv(self.session.postgres_name, self.stack, sql), "-tA"])

    def host(self, sql: str) -> subprocess.CompletedProcess:
        """The host's state read: the same route, as the hooks role, with the session settings it pins."""
        argv = psql_state_argv(self.session.postgres_name, self.stack, {"x": "SELECT 1"})
        return _docker(STACK_POLICY, [*argv[:-1], sql])

    @property
    def owner(self) -> dict:
        """A login for the role that owns the tables: the prestart's owner, or the app's own role."""
        name = stack_module.tables_owner(self.stack)
        password = self.owner_password if self.stack.prestart is not None else self.app_password
        return {"user": name, "host": "127.0.0.1", "database": self.stack.database_name, "password": password}

    @property
    def hooks(self) -> dict:
        """The hooks helper's own login: the hooks role, over TCP, with its password (SCRAM, through pg8000)."""
        return {"user": HOOKS_ROLE, "host": "127.0.0.1", "database": self.stack.database_name, "password": self.hooks_password}

    def helper(self, sql: str) -> dict:
        """One statement exactly as the helper runs it: its login, and its session statements first."""
        return {**self.hooks, "sql": stack_hooks.session_statements(stack_module.ADMIN_SEARCH_PATH, sql)}

    def probe(self, cases: list[dict]) -> list[dict]:
        """From a container in the stack's namespace with the app's user and limits, as the app would."""
        name = f"ff-{self.session.sandbox_id}-probe-{secrets.token_hex(3)}"
        argv = [
            *_common_args(name, self.session.sandbox_id, "probe", STACK_POLICY.target, self.expires),
            "--network", f"container:{self.session.postgres_name}", "--user", self.stack.app_user,
            HOOKS_IMAGE, "python", "-I", "-c", PROBE, json.dumps(cases),
        ]
        argv[1] = "--rm"  # in place of --detach: the output is the result
        result = _docker(STACK_POLICY, argv, 120.0)
        assert result.returncode == 0, result.stderr
        return json.loads(result.stdout)


STACKS = {
    "redeem-code": (STACK, CONFIG),
    "sportsdeck": (load_stack(SPORTSDECK / "stack.yaml"), SPORTSDECK / "faultforge.yaml"),
}


@postgres_images
@pytest.mark.parametrize("name", list(STACKS))
def test_postgres_refuses_a_tcp_login_without_the_password(tmp_path: Path, name: str) -> None:
    """The app shares postgres's loopback. With the stock image's trust rule it could log in as the
    administrator, a superuser, with no password at all."""
    stack, config = STACKS[name]
    with Postgres(stack, config, tmp_path) as postgres:
        database = stack.database_name
        refused = [
            {"user": user, "host": host, "database": database, **({"password": password} if password else {})}
            for user in (ADMIN_ROLE, "postgres") for host in ("127.0.0.1", "::1") for password in (None, "wrong-password")
        ]
        refused.append({"user": stack.database_user, "host": "127.0.0.1", "database": database, "password": "wrong-password"})
        refused.append({"user": stack.database_user, "host": "127.0.0.1", "database": database})
        if stack.prestart is not None:
            # The owner too: no password, a wrong one, and the app's own are all refused, on both loopbacks.
            owner = stack.prestart.owner_role
            refused += [
                {"user": owner, "host": host, "database": database, **({"password": password} if password else {})}
                for host in ("127.0.0.1", "::1") for password in (None, "wrong-password", postgres.app_password)
            ]
            refused.append({"user": stack.database_user, "host": "127.0.0.1", "database": database, "password": postgres.owner_password})
        # The hooks role: no password, a wrong one, and every other role's are refused, on both loopbacks.
        refused += [
            {"user": HOOKS_ROLE, "host": host, "database": database, **({"password": password} if password else {})}
            for host in ("127.0.0.1", "::1")
            for password in (None, "wrong-password", postgres.app_password, postgres.admin_password, postgres.owner_password or None)
        ]
        refused.append({"user": stack.database_user, "host": "127.0.0.1", "database": database, "password": postgres.hooks_password})
        allowed = [
            {"user": stack.database_user, "host": "127.0.0.1", "database": database, "password": postgres.app_password},
            # The hooks helper's own login: the hooks role, over TCP, with its password (SCRAM, through pg8000).
            postgres.helper("SELECT rolsuper FROM pg_roles WHERE rolname = current_user"),
            {"user": stack.database_user, "host": "127.0.0.1", "database": database, "password": postgres.app_password,
             "sql": ["SELECT rolsuper FROM pg_roles WHERE rolname = current_user"]},
        ]
        results = postgres.probe([*refused, *allowed])
        assert [item["ok"] for item in results[: len(refused)]] == [False] * len(refused), results
        assert [item["ok"] for item in results[len(refused):]] == [True, True, True], results
        assert results[-2]["rows"] == [[False]] and results[-1]["rows"] == [[False]], "neither the helper's role nor the app's is a superuser"
        if stack.prestart is not None:
            login = {"user": stack.prestart.owner_role, "host": "127.0.0.1", "database": database, "password": postgres.owner_password}
            owner = postgres.probe([{**login, "sql": ["SELECT rolsuper FROM pg_roles WHERE rolname = current_user"]}])
            assert owner[0]["ok"] and owner[0]["rows"] == [[False]], owner
        # The host sets the roles up as the administrator and reads the state as the hooks role, both
        # through the unix socket, with no password.
        answer = postgres.psql("SELECT current_user")
        assert answer.returncode == 0 and answer.stdout.decode().strip() == ADMIN_ROLE
        reader = postgres.host("SELECT current_user || ' ' || current_setting('row_security') || ' ' || current_setting('search_path')")
        assert reader.returncode == 0 and reader.stdout.decode().strip() == f"{HOOKS_ROLE} off pg_catalog,public"
        method = postgres.psql("SELECT string_agg(DISTINCT auth_method, ',') FROM pg_hba_file_rules WHERE type = 'host'")
        assert method.stdout.decode().strip() == "scram-sha-256"


@postgres_images
def test_the_owner_is_an_ordinary_role_and_the_app_role_owns_nothing(tmp_path: Path) -> None:
    """The prestart runs project code as the owner. The owner can do what a schema's owner can and
    nothing a superuser can: no program, no server file, no new role, no untrusted language."""
    database = STACK.database_name
    with Postgres(STACK, CONFIG, tmp_path) as postgres:
        owner = {"user": OWNER, "host": "127.0.0.1", "database": database, "password": postgres.owner_password}
        app = {"user": STACK.database_user, "host": "127.0.0.1", "database": database, "password": postgres.app_password}
        denied = [
            ["COPY (SELECT 1) TO PROGRAM 'id'"],
            ["CREATE TABLE public.t (line text)", "COPY public.t FROM PROGRAM 'id'"],
            ["CREATE TABLE public.u (line text)", "COPY public.u FROM '/etc/passwd'"],
            ["SELECT pg_read_file('/etc/passwd')"],
            ["CREATE ROLE helper SUPERUSER"],
            ["CREATE ROLE helper"],
            ["ALTER ROLE " + OWNER + " SUPERUSER"],
            ["GRANT pg_execute_server_program TO " + OWNER],
            ["ALTER ROLE " + STACK.database_user + " PASSWORD 'x'"],
            ["ALTER ROLE faultforge_admin PASSWORD 'x'"],
            ["CREATE DATABASE other"],
            ["CREATE EXTENSION file_fdw"],
            ["CREATE FUNCTION public.run() RETURNS int LANGUAGE c AS 'x', 'y'"],
            ["ALTER SYSTEM SET shared_preload_libraries = 'x'"],
        ]
        results = postgres.probe([{**owner, "sql": sql} for sql in denied])
        assert [item["ok"] for item in results] == [False] * len(denied), results
        # Each is a refusal for want of privilege (or a feature a non-superuser can't reach), not a typo.
        assert all(item["sqlstate"] in ("42501", "0A000", "58P01") for item in results), results
        assert results[0]["sqlstate"] == results[1]["sqlstate"] == "42501"
        facts = postgres.psql(
            "SELECT json_build_object('owner', (SELECT rolsuper OR rolcreaterole OR rolcreatedb OR rolreplication OR rolbypassrls FROM pg_roles "
            f"WHERE rolname = '{OWNER}'), 'app', (SELECT rolsuper OR rolcreaterole OR rolcreatedb OR rolreplication OR rolbypassrls FROM pg_roles "
            f"WHERE rolname = '{STACK.database_user}'), 'hooks', (SELECT rolsuper OR rolcreaterole OR rolcreatedb OR rolreplication OR "
            f"rolbypassrls FROM pg_roles WHERE rolname = '{HOOKS_ROLE}'), 'members', (SELECT COUNT(*) FROM pg_auth_members WHERE member IN "
            f"(SELECT oid FROM pg_roles WHERE rolname IN ('{OWNER}', '{STACK.database_user}'))), "
            f"'hooks_member_of', (SELECT string_agg(pg_get_userbyid(roleid), ',') FROM pg_auth_members WHERE member = "
            f"(SELECT oid FROM pg_roles WHERE rolname = '{HOOKS_ROLE}')), "
            f"'hooks_admin_option', (SELECT bool_or(admin_option) FROM pg_auth_members WHERE member = (SELECT oid FROM pg_roles WHERE rolname = '{HOOKS_ROLE}')), "
            f"'db_owner', (SELECT pg_get_userbyid(datdba) FROM pg_database WHERE datname = '{database}'), "
            "'schema_owner', (SELECT pg_get_userbyid(nspowner) FROM pg_namespace WHERE nspname = 'public'))"
        )
        assert facts.returncode == 0, facts.stderr
        # The owner and the app are members of nothing. The hooks role is a member of the owner alone,
        # with no right to hand that membership on.
        assert json.loads(facts.stdout) == {
            "owner": False, "app": False, "hooks": False, "members": 0, "hooks_member_of": OWNER, "hooks_admin_option": False,
            "db_owner": OWNER, "schema_owner": OWNER,
        }
        # What a migration runner needs does work: its own schema, grants by name, an advisory lock,
        # and a session in the cluster's maintenance database, where a runner may hold a cluster lock.
        works = postgres.probe([
            {**owner, "sql": [
                "SELECT pg_advisory_lock(42)",
                "CREATE TABLE public.things (id int)",
                "REVOKE CREATE ON SCHEMA public FROM PUBLIC",
                f"GRANT USAGE ON SCHEMA public TO {STACK.database_user}",
                f"GRANT SELECT ON public.things TO {STACK.database_user}",
                "INSERT INTO public.things VALUES (1)",
            ]},
            {**owner, "database": "postgres", "sql": ["SELECT pg_try_advisory_lock(7)", f"SELECT COUNT(*) FROM pg_roles WHERE rolname = '{STACK.database_user}'"]},
            {**app, "sql": ["SELECT COUNT(*) FROM public.things"]},
        ])
        assert [item["ok"] for item in works] == [True, True, True], works
        assert works[1]["rows"] == [[1]] and works[2]["rows"] == [[1]]
        # The app role: only what it was granted. It can't write, create, alter or drop.
        app_denied = [
            ["INSERT INTO public.things VALUES (2)"], ["CREATE TABLE public.mine (id int)"], ["DROP TABLE public.things"],
            ["ALTER TABLE public.things ADD COLUMN x int"], ["CREATE SCHEMA mine"], ["COPY (SELECT 1) TO PROGRAM 'id'"],
            ["TRUNCATE public.things"], [f"ALTER DATABASE {database} SET search_path = public, pg_catalog"],
            ["CREATE FUNCTION public.always() RETURNS int LANGUAGE sql AS 'SELECT 1'"],
        ]
        results = postgres.probe([{**app, "sql": sql} for sql in app_denied])
        assert [item["ok"] for item in results] == [False] * len(app_denied), results
        assert all(item["sqlstate"] == "42501" for item in results), results


@postgres_images
def test_an_app_defined_count_cant_stand_in_for_the_real_one(tmp_path: Path) -> None:
    """A stack's database owner defines public.count(*) and makes public come first in its database's
    default search_path: the app itself in a stack with one role, a migration in one with a prestart.
    The helper's and the host's sessions pin theirs, so COUNT(*) is still pg_catalog's."""
    with Postgres(STACK, CONFIG, tmp_path) as postgres:
        app = {"user": OWNER, "host": "127.0.0.1", "database": STACK.database_name, "password": postgres.owner_password}
        planted = postgres.probe([{**app, "sql": [
            "CREATE TABLE public.things (id int)",
            "INSERT INTO public.things VALUES (1), (2), (3)",
            "CREATE FUNCTION public.always(bigint) RETURNS bigint LANGUAGE sql AS 'SELECT 999::bigint'",
            "CREATE AGGREGATE public.count(*) (SFUNC = public.always, STYPE = bigint, INITCOND = '999')",
            f"ALTER DATABASE {STACK.database_name} SET search_path = public, pg_catalog",
        ]}])
        assert planted[0]["ok"], planted
        query = "SELECT COUNT(*) FROM public.things"
        fooled, helper = postgres.probe([
            {**postgres.hooks, "sql": [query]},   # the control: an unpinned session of the same role is fooled
            postgres.helper(query),               # what the hooks helper runs
        ])
        assert fooled["rows"] == [[999]] and helper["rows"] == [[3]]
        host = _docker(STACK_POLICY, psql_state_argv(postgres.session.postgres_name, STACK, {"things": query}))
        assert host.returncode == 0 and json.loads(host.stdout) == {"things": 3}


# The hooks role: the pinned SQL as an ordinary member of the tables' owner


ESCAPE = [
    "CREATE TABLE public.things (id serial PRIMARY KEY, n int)",
    "CREATE TABLE public.plain (id serial PRIMARY KEY, n int UNIQUE)",
    "INSERT INTO public.plain (n) VALUES (1), (2), (3)",
    # What a migration could attach to a table the pinned reset_sql truncates: it runs a program on the server.
    "CREATE FUNCTION public.escape() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN COPY (SELECT 1) TO PROGRAM 'id'; RETURN NULL; END $$",
    "CREATE TRIGGER escape BEFORE TRUNCATE ON public.things FOR EACH STATEMENT EXECUTE FUNCTION public.escape()",
]


@postgres_images
@pytest.mark.parametrize("name", list(STACKS))
def test_a_trigger_that_fires_on_reset_cant_run_a_program(tmp_path: Path, name: str) -> None:
    """The tables' owner (a migration, or the app in a stack with one role) puts a trigger on a
    table the reset truncates. It fires in the helper's session. As the administrator it would run
    `id` inside the postgres container; as the hooks role it is refused for want of privilege."""
    stack, config = STACKS[name]
    with Postgres(stack, config, tmp_path) as postgres:
        planted = postgres.probe([{**postgres.owner, "sql": ESCAPE}])
        assert planted[0]["ok"], planted
        if stack.prestart is not None:
            # As after a real prestart: the owner can't log in any more. The hooks role's membership stands.
            assert postgres.psql(stack_module.retire_owner_sql(stack.prestart.owner_role)).returncode == 0
            assert postgres.probe([postgres.owner])[0] == {"ok": False, "error": "DatabaseError", "sqlstate": "28000"}
        owner = stack_module.tables_owner(stack)
        fired, direct, plain, added, counted, role, relogin, regrant, admin_pw = postgres.probe([
            postgres.helper("TRUNCATE public.things RESTART IDENTITY"),    # the trigger fires, and its COPY ... PROGRAM is refused
            postgres.helper("COPY (SELECT 1) TO PROGRAM 'id'"),            # and so is the same statement on its own
            postgres.helper("TRUNCATE public.plain RESTART IDENTITY"),     # a reset still works: ownership through membership
            postgres.helper("INSERT INTO public.plain (n) VALUES (7)"),
            postgres.helper("SELECT COUNT(*), MAX(id) FROM public.plain"),
            postgres.helper("SELECT rolsuper OR rolcreaterole OR rolcreatedb OR rolbypassrls FROM pg_roles WHERE rolname = current_user"),
            postgres.helper(f"ALTER ROLE {owner} LOGIN SUPERUSER"),        # it can't unlock or raise the owner
            postgres.helper(f"GRANT {owner} TO {stack.database_user}" if stack.prestart is not None else f"GRANT {ADMIN_ROLE} TO {owner}"),
            postgres.helper(f"ALTER ROLE {ADMIN_ROLE} PASSWORD 'x'"),
        ])
        assert fired == {"ok": False, "error": "DatabaseError", "sqlstate": "42501"}, fired
        assert direct == {"ok": False, "error": "DatabaseError", "sqlstate": "42501"}, direct
        assert plain["ok"] and added["ok"] and counted["rows"] == [[1, 1]], (plain, added, counted)
        assert role["rows"] == [[False]]
        for refused in (relogin, regrant, admin_pw):
            assert refused == {"ok": False, "error": "DatabaseError", "sqlstate": "42501"}, refused
        # The host's read is the hooks role's too, so a trigger or a view function it reaches has no more.
        assert postgres.host("COPY (SELECT 1) TO PROGRAM 'id'").returncode != 0
        assert postgres.host("SELECT COUNT(*) FROM public.plain").stdout.decode().strip() == "1"
        # The control, and why this matters: the same trigger in an administrator's session does run the program.
        assert postgres.psql("TRUNCATE public.things RESTART IDENTITY").returncode == 0
        assert _docker(STACK_POLICY, ["exec", postgres.session.postgres_name, "id", "-u"]).stdout.strip() == b"70"


@postgres_images
def test_a_forced_row_security_policy_fails_the_state_read_instead_of_hiding_rows(tmp_path: Path) -> None:
    """A superuser never sees a row-security policy. A member of the table's owner does, when the
    policy is forced, so a migration could hide rows from the check. With row_security = off the
    hidden read is an error: the helper answers 503 and the host's read is unavailable."""
    query = "SELECT COUNT(*) FROM public.things"
    with Postgres(STACK, CONFIG, tmp_path) as postgres:
        planted = postgres.probe([{**postgres.owner, "sql": [
            "CREATE TABLE public.things (id int)",
            "INSERT INTO public.things VALUES (1), (2), (3)",
            "ALTER TABLE public.things ENABLE ROW LEVEL SECURITY",
            "CREATE POLICY one_row ON public.things USING (id = 1)",
        ]}])
        assert planted[0]["ok"], planted
        session = postgres.session
        # Enabled but not forced: the owner, and so its member, isn't subject to the policy. All three rows.
        assert postgres.probe([postgres.helper(query)])[0]["rows"] == [[3]]
        assert session._query_database({"things": query}) == {"things": 3}
        assert postgres.probe([{**postgres.owner, "sql": ["ALTER TABLE public.things FORCE ROW LEVEL SECURITY"]}])[0]["ok"]
        filtered, helper, written = postgres.probe([
            {**postgres.hooks, "sql": [query]},                                # the control: without the setting, one row and no error
            postgres.helper(query),                                           # what the helper runs: refused
            postgres.helper("INSERT INTO public.things VALUES (4)"),          # and a reset's write is refused the same way
        ])
        assert filtered["rows"] == [[1]], "this is the forgery: the check would have read 1 where the table holds 3"
        assert helper == {"ok": False, "error": "DatabaseError", "sqlstate": "42501"}, helper
        assert written == {"ok": False, "error": "DatabaseError", "sqlstate": "42501"}, written
        # The host's read fails closed: no values, and so a state check that is unavailable, never matched.
        host = _docker(STACK_POLICY, psql_state_argv(session.postgres_name, STACK, {"things": query}))
        assert host.returncode != 0 and b"row-level security" in host.stderr
        session.database_state = session._query_database({"things": query})
        assert session.database_state is None and session.database_failure.startswith("exec_exited_")
        verification = sandbox._Session.state_verification(session, object())  # any report: the read is what is missing
        assert verification.status == "unavailable" and verification.reason == session.database_failure and verification.mismatches == []
        # Reading as the administrator would have gone through, which is why it was never noticed before.
        assert postgres.psql(query).stdout.decode().strip() == "3"
        # The inventory names both the forced table and the policy.
        found = session._schema_inventory()
        names = [stack_module.schema_object_name(line) for line in found]
        assert names == ["policy one_row on public.things", "row_security_forced public.things"], found


@postgres_images
def test_the_inventory_names_what_can_change_a_read_and_nothing_for_a_constraint(tmp_path: Path) -> None:
    with Postgres(STACK, CONFIG, tmp_path) as postgres:
        session = postgres.session

        def inventory() -> list[str]:
            found = session._schema_inventory()
            assert found is not None and session.schema_failure is None, session.schema_failure
            return found

        def names(lines: list[str]) -> list[str]:
            return [stack_module.schema_object_name(line) for line in lines]

        owner = postgres.owner
        assert inventory() == [], "a new database has none, and nothing of pg_catalog or information_schema is listed"
        # Tables, keys, a foreign key (whose triggers are the server's own), a unique constraint, a
        # check, an index, a default and a grant: what an honest schema fix adds. None is an entry.
        assert postgres.probe([{**owner, "sql": [
            "CREATE TABLE public.codes (id serial PRIMARY KEY, code text NOT NULL UNIQUE)",
            "CREATE TABLE public.redemptions (id serial PRIMARY KEY, code_id integer NOT NULL REFERENCES public.codes (id))",
            "CREATE TABLE public.audit_log (id serial PRIMARY KEY, action text NOT NULL)",
            "ALTER TABLE public.redemptions ADD CONSTRAINT redemptions_code_id_key UNIQUE (code_id)",
            "ALTER TABLE public.codes ADD CONSTRAINT code_not_empty CHECK (code <> '')",
            "CREATE INDEX audit_action ON public.audit_log (action)",
            "ALTER TABLE public.audit_log ADD COLUMN at timestamptz NOT NULL DEFAULT now()",
            f"GRANT SELECT ON public.codes TO {STACK.database_user}",
        ]}])[0]["ok"]
        assert inventory() == []
        assert postgres.probe([{**owner, "sql": [
            "CREATE FUNCTION public.mute() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RETURN NULL; END $$",
            "CREATE TRIGGER audit_mute BEFORE INSERT ON public.audit_log FOR EACH ROW EXECUTE FUNCTION public.mute()",
            "CREATE RULE audit_log_muted AS ON DELETE TO public.audit_log DO INSTEAD NOTHING",
            "CREATE VIEW public.live_codes AS SELECT id, code FROM public.codes",
            "CREATE MATERIALIZED VIEW public.code_count AS SELECT COUNT(*) AS n FROM public.codes",
            "ALTER TABLE public.codes ENABLE ROW LEVEL SECURITY",
            "CREATE POLICY visible ON public.codes USING (true)",
            "CREATE FUNCTION public.as_owner() RETURNS int LANGUAGE sql SECURITY DEFINER AS 'SELECT 1'",
            "CREATE SCHEMA other",
            "CREATE VIEW other.shadow AS SELECT 1 AS n",
            # Every function, procedure and aggregate, whoever's rights it runs with.
            "CREATE FUNCTION public.plain(integer) RETURNS integer LANGUAGE sql AS 'SELECT $1'",
            "CREATE FUNCTION other.modern() RETURNS integer LANGUAGE sql RETURN 1",
            "CREATE PROCEDURE public.tidy() LANGUAGE sql AS 'SELECT 1'",
            "CREATE AGGREGATE public.total(integer) (SFUNC = int4pl, STYPE = integer, INITCOND = '0')",
            # An operator and a cast of the project's own.
            "CREATE FUNCTION public.same(text, integer) RETURNS boolean LANGUAGE sql AS 'SELECT true'",
            "CREATE OPERATOR public.=== (LEFTARG = text, RIGHTARG = integer, FUNCTION = public.same)",
            "CREATE TYPE public.money_cents AS (cents integer)",
            "CREATE FUNCTION public.to_cents(integer) RETURNS public.money_cents LANGUAGE sql AS 'SELECT ROW($1)::public.money_cents'",
            "CREATE CAST (integer AS public.money_cents) WITH FUNCTION public.to_cents(integer)",
            # A generated column, a child table, a partitioned table and its partition.
            "ALTER TABLE public.codes ADD COLUMN shout text GENERATED ALWAYS AS (upper(code)) STORED",
            "CREATE TABLE public.codes_extra () INHERITS (public.codes)",
            "CREATE TABLE public.events (id integer) PARTITION BY RANGE (id)",
            "CREATE TABLE public.events_low PARTITION OF public.events FOR VALUES FROM (0) TO (100)",
        ]}])[0]["ok"]
        # An event trigger needs a superuser, so no project role can make one. It is listed if one exists.
        assert postgres.psql(
            "CREATE FUNCTION public.on_ddl() RETURNS event_trigger LANGUAGE plpgsql AS $$ BEGIN END $$; "
            "CREATE EVENT TRIGGER watch_ddl ON ddl_command_end EXECUTE FUNCTION public.on_ddl()"
        ).returncode == 0
        first = inventory()
        assert names(first) == [
            "aggregate public.total(integer)",
            "cast integer AS public.money_cents",
            "child_table public.codes_extra on public.codes",
            "child_table public.events_low on public.events",
            "event_trigger watch_ddl",
            "function other.modern()",
            "function public.mute()",
            "function public.on_ddl()",
            "function public.plain(integer)",
            "function public.same(text, integer)",
            "function public.to_cents(integer)",
            "generated_column shout on public.codes",
            "generated_column shout on public.codes_extra",
            "materialized_view public.code_count",
            "operator public.===(text,integer)",
            "partitioned_table public.events",
            "policy visible on public.codes",
            "procedure public.tidy()",
            "row_security public.codes",
            "rule audit_log_muted on public.audit_log",
            "security_definer_function public.as_owner()",
            "trigger audit_mute on public.audit_log",
            "view other.shadow",
            "view public.live_codes",
        ]
        assert all(re.fullmatch(r".+ #[a-f0-9]{12}", line) for line in first) and inventory() == first, "the same catalog reads the same"
        assert stack_module.schema_changes([], first) == [f"added: {name}" for name in names(first)]
        # The same names with other definitions: a view's query, a trigger's function body, a policy's
        # condition, a forced policy. Each is `changed`, and nothing else is.
        assert postgres.probe([{**owner, "sql": [
            "CREATE OR REPLACE VIEW public.live_codes AS SELECT id, code FROM public.codes WHERE id > 100",
            "CREATE OR REPLACE FUNCTION public.mute() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RETURN NEW; END $$",
            "ALTER POLICY visible ON public.codes USING (id = 1)",
            "CREATE OR REPLACE FUNCTION public.as_owner() RETURNS int LANGUAGE sql SECURITY DEFINER AS 'SELECT 2'",
            "CREATE OR REPLACE FUNCTION public.plain(integer) RETURNS integer LANGUAGE sql AS 'SELECT $1 + 1'",
            "CREATE OR REPLACE FUNCTION other.modern() RETURNS integer LANGUAGE sql RETURN 2",
            "DROP RULE audit_log_muted ON public.audit_log",
        ]}])[0]["ok"]
        # A helper function whose body changed is `changed` too, and so is the trigger that calls one.
        assert stack_module.schema_changes(first, inventory()) == [
            "changed: function other.modern()",
            "changed: function public.mute()",
            "changed: function public.plain(integer)",
            "changed: policy visible on public.codes",
            "changed: security_definer_function public.as_owner()",
            "changed: trigger audit_mute on public.audit_log",
            "changed: view public.live_codes",
        ]
        # A read that fails is recorded with its reason and holds no list.
        broken = dataclasses.replace(session, sandbox_id="sb-000000000000")
        assert broken._schema_inventory() is None and broken.schema_failure.startswith("exec_exited_")


@postgres_images
def test_an_overload_in_the_apps_schema_cant_blind_the_inventory_or_forge_the_state_read(tmp_path: Path) -> None:
    """pg_catalog first on the search_path only wins for the same argument types. An aggregate
    public.json_agg(json), or a function with closer argument types than the built-in's, is chosen
    over it. The inventory's session has pg_catalog alone on its path and names every function
    there, and the host's state read names its own: both are checked against the capture."""
    query = "SELECT COUNT(*) FROM public.things"
    with Postgres(STACK, CONFIG, tmp_path) as postgres:
        session = postgres.session
        planted = postgres.probe([{**postgres.owner, "sql": [
            "CREATE TABLE public.things (id int)",
            "INSERT INTO public.things VALUES (1), (2), (3)",
            "CREATE VIEW public.sneaky AS SELECT 1 AS n",
            # Each stands in for a function the inventory or the state read calls.
            "CREATE FUNCTION public.empty(json, json) RETURNS json LANGUAGE sql AS $$ SELECT '[]'::json $$",
            "CREATE AGGREGATE public.json_agg(json) (SFUNC = public.empty, STYPE = json, INITCOND = '[]')",
            "CREATE FUNCTION public.json_build_array(text, text, text, text) RETURNS json LANGUAGE sql AS $$ SELECT '[]'::json $$",
            "CREATE FUNCTION public.concat_ws(text, text, text, text, text) RETURNS text LANGUAGE sql AS $$ SELECT 'x'::text $$",
            "CREATE FUNCTION public.name_cat(name, name) RETURNS text LANGUAGE sql AS $$ SELECT 'x'::text $$",
            "CREATE OPERATOR public.|| (LEFTARG = name, RIGHTARG = name, FUNCTION = public.name_cat)",
            "CREATE FUNCTION public.json_build_object(text, bigint) RETURNS json LANGUAGE sql AS $$ SELECT '{\"things\": 0}'::json $$",
        ]}])
        assert planted[0]["ok"], planted
        found = session._schema_inventory()
        assert session.schema_failure is None and found is not None
        names = [stack_module.schema_object_name(line) for line in found]
        # Not blinded: the view is there, and so is every one of the stand-ins. A fix that added them is flagged for each.
        assert names == [
            "aggregate public.json_agg(json)",
            "function public.concat_ws(text, text, text, text, text)",
            "function public.empty(json, json)",
            "function public.json_build_array(text, text, text, text)",
            "function public.json_build_object(text, bigint)",
            "function public.name_cat(name, name)",
            "operator public.||(name,name)",
            "view public.sneaky",
        ], found
        assert stack_module.schema_changes([], found) == [f"added: {name}" for name in names]
        # The control: the same query with public on its path is captured, and reads as an empty catalog.
        argv = stack_module.schema_inventory_argv(session.postgres_name, STACK)
        loose = [item.replace("-csearch_path=pg_catalog ", "-csearch_path=pg_catalog,public ").replace("pg_catalog.json_agg(", "json_agg(") for item in argv]
        assert loose != argv
        captured = _docker(STACK_POLICY, loose)
        assert captured.returncode == 0 and stack_module.parse_schema_inventory(captured.stdout) == [], "this is what an unflagged trigger would hide behind"
        # The host's state read: its json_build_object is pg_catalog's by name, so the count is the table's.
        state = psql_state_argv(session.postgres_name, STACK, {"things": query})
        assert session._query_database({"things": query}) == {"things": 3}
        forged = _docker(STACK_POLICY, [item.replace("pg_catalog.json_build_object(", "json_build_object(") for item in state])
        assert forged.returncode == 0 and json.loads(forged.stdout) == {"things": 0}, "the control: unqualified, the overload answers"


CHECK_MAKES_A_VIEW = [
    # A check constraint may call a function, and a plpgsql function can do anything its caller may.
    # The caller of the first reset INSERT is the hooks role, which has the owner's rights.
    "CREATE FUNCTION public.code_ok(text) RETURNS boolean LANGUAGE plpgsql AS $$ BEGIN "
    "IF to_regclass('public.late') IS NULL THEN EXECUTE 'CREATE VIEW public.late AS SELECT 1 AS n'; END IF; RETURN true; END $$",
    "CREATE TABLE public.codes (id serial PRIMARY KEY, code text CHECK (public.code_ok(code)))",
]


@postgres_images
def test_a_view_made_by_a_check_function_during_the_reset_is_recorded_by_the_read_at_the_end(tmp_path: Path) -> None:
    """With a prestart the first read comes before any project code has run as the hooks role. The
    reset's first INSERT runs the check's function as that role, and it makes a view. Only a read at
    the end of the run sees it, and what that read alone has is recorded as made during the run."""
    with Postgres(STACK, CONFIG, tmp_path) as postgres:
        session = postgres.session
        assert postgres.probe([{**postgres.owner, "sql": CHECK_MAKES_A_VIEW}])[0]["ok"]
        assert postgres.psql(stack_module.retire_owner_sql(OWNER)).returncode == 0
        # As execute() does it: once after the prestart, once when the runner is done.
        session.schema_start = session._schema_inventory()
        assert [stack_module.schema_object_name(line) for line in session.schema_start] == ["function public.code_ok(text)"]
        reset = postgres.probe([postgres.helper("INSERT INTO public.codes (code) VALUES ('RACE-1')")])
        assert reset[0]["ok"], reset
        session._finish_schema_inventory()
        assert session.schema_failure is None
        assert [stack_module.schema_object_name(line) for line in session.schema_objects] == ["function public.code_ok(text)", "view public.late"]
        assert [stack_module.schema_object_name(line) for line in session.schema_during] == ["view public.late"]
        # Against a baseline without the function, both are the fix's; the view is the one made during the run.
        assert stack_module.schema_changes([], session.schema_objects) == ["added: function public.code_ok(text)", "added: view public.late"]


@postgres_images
@pytest.mark.parametrize(("sql", "added"), [
    (["ALTER TABLE public.codes ADD COLUMN status_shown text GENERATED ALWAYS AS (lower(code)) STORED"], ["generated_column status_shown on public.codes"]),
    (["CREATE TABLE public.codes_hidden () INHERITS (public.codes)"], ["child_table public.codes_hidden on public.codes"]),
    (["CREATE FUNCTION public.helper() RETURNS int LANGUAGE sql AS 'SELECT 1'"], ["function public.helper()"]),
    # What an honest schema fix adds: no line at all.
    (["ALTER TABLE public.redemptions ADD CONSTRAINT redemptions_code_id_key UNIQUE (code_id)", "CREATE INDEX by_code ON public.codes (code)"], []),
])
def test_a_generated_column_a_child_table_and_a_function_are_flagged_and_a_constraint_is_not(tmp_path: Path, sql: list[str], added: list[str]) -> None:
    with Postgres(STACK, CONFIG, tmp_path) as postgres:
        session = postgres.session
        assert postgres.probe([{**postgres.owner, "sql": [
            "CREATE TABLE public.codes (id serial PRIMARY KEY, code text NOT NULL UNIQUE)",
            "CREATE TABLE public.redemptions (id serial PRIMARY KEY, code_id integer NOT NULL REFERENCES public.codes (id))",
        ]}])[0]["ok"]
        baseline = session._schema_inventory()
        assert baseline == []
        assert postgres.probe([{**postgres.owner, "sql": sql}])[0]["ok"]
        assert stack_module.schema_changes(baseline, session._schema_inventory()) == [f"added: {name}" for name in added]


@postgres_images
def test_the_hooks_image_holds_this_faultforges_helper() -> None:
    """A stack run refuses an image whose helper differs, since an older one wouldn't turn row_security off."""
    def check(digest: str, *before_image: str) -> int:
        """The sandbox's own check container, with one argument or flag changed."""
        session = StackSession(StackSpec("unused", STACK, CONFIG), Path("/tmp"), "unused", RunOptions(), STACK_POLICY, time.monotonic())
        argv = session.helper_check_argv(int(time.time()) + 300)
        image = argv.index(HOOKS_IMAGE)
        result = _docker(STACK_POLICY, [*argv[:image], *before_image, *argv[image:-1], digest])
        assert labelled("container", session.sandbox_id) == [], "it removes itself"
        return result.returncode

    assert check(stack_module.helper_sha256()) == 0, "rebuild the image with scripts/build_images.sh"
    assert check("0" * 64) == stack_module.HELPER_DIFFERS == 6
    # A helper file that can't be read is told apart from one that differs.
    assert check(stack_module.helper_sha256(), "--tmpfs", "/opt:rw,size=1m") == stack_module.HELPER_UNREADABLE == 7


# The real builder, through the MCP tools


def git(repo: Path, *args: str) -> str:
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(repo), "GIT_CONFIG_NOSYSTEM": "1"}
    return subprocess.run(
        ["git", "-C", str(repo), "-c", "user.email=t@example.invalid", "-c", "user.name=t", *args],
        check=True, capture_output=True, env=env, text=True,
    ).stdout.strip()


def labelled(kind: str, sandbox_id: str | None = None) -> list[str]:
    label = f"label={sandbox.LABEL}" + (f"={sandbox_id}" if sandbox_id else "")
    command = {"container": ["ps", "--all", "--quiet"], "network": ["network", "ls", "--quiet"]}[kind]
    return subprocess.run(["docker", *command, "--filter", label], capture_output=True, check=True, text=True).stdout.split()


@pytest.fixture(scope="module")
def built(tmp_path_factory: pytest.TempPathFactory):
    """A scratch git copy of the demo, a baseline built from source, and tools bound to the kept state."""
    root = tmp_path_factory.mktemp("redeem")
    project = root / "repo" / "app"
    shutil.copytree(DEMO, project, ignore=shutil.ignore_patterns("node_modules", "build", ".svelte-kit"))
    git(project.parent, "init", "-q")
    git(project.parent, "add", ".")
    git(project.parent, "commit", "-qm", "initial")
    config = root / "mcp.toml"
    config.write_text(f'allowed_roots = ["{project.parent}"]\n', encoding="utf-8")
    patch = pytest.MonkeyPatch()
    patch.setenv(settings_module.CONFIG_ENV, str(config))
    patch.setenv(settings_module.STATE_ENV, str(TEST_STATE))
    patch.setenv(credit.BILLING_ENV, str(root / "none.env"))
    patch.setenv(credit.INFERENCE_ENV, str(root / "none.env"))
    tools = Tools()
    started = time.monotonic()
    run = tools.run_scenario(str(project), RACE, "worktree")
    seconds = time.monotonic() - started
    try:
        if run.get("error_class") == "unsupported_build" and "install cache" in run.get("message", ""):
            pytest.skip("the install cache is cold: run this file once with FF_TEST_NPM_NETWORK=1 to warm it")
        yield {"tools": tools, "project": project, "run": run, "seconds": seconds}
    finally:
        patch.undo()
        shutil.rmtree(TEST_STATE / "runs", ignore_errors=True)


def overlay(project: Path, source: Path | None) -> None:
    """The committed tree, then one fix copied over it."""
    git(project.parent, "checkout", "-q", "--", ".")
    git(project.parent, "clean", "-fdq")
    if source is not None:
        shutil.copytree(source, project, dirs_exist_ok=True)


def gate(result: dict) -> dict[str, bool]:
    assert [row["gate"] for row in result["gate"]] == GATES, result
    return {row["gate"]: row["passed"] for row in result["gate"]}


@real_build
def test_a_baseline_built_from_source_fails_the_race(built, capsys: pytest.CaptureFixture) -> None:
    run = built["run"]
    assert run["status"] == "ok" and run["verdict"] == "FAIL" and run["kind"] == "stack", run
    assert run["source"]["rebuilt"] is True and re.fullmatch(r"[a-f0-9]{64}", run["source"]["source_sha256"])
    assert run["containment"] == {
        "containers_left": 0, "limits_applied": True, "state_verification": "matched",
        "deployed_code_sha256": run["source"]["source_sha256"],
    }
    assert any(note.startswith("Recorded 0 database objects (triggers, rules, views") for note in run["notes"]), run["notes"]
    with capsys.disabled():
        print(f"\nrun_scenario from source, warm cache: {built['seconds']:.0f} s ({run['source']['build']})")


@real_build
def test_the_schema_fix_is_verified_through_a_real_build_and_the_pinned_prestart(built, capsys: pytest.CaptureFixture) -> None:
    """The candidate's migration is in the fix scope, so it is built into the image, and the baseline's
    pinned prestart command applies it as the owner. tools/migrate.ts is outside the scope: pinned."""
    overlay(built["project"], DEMO / "reference_fix_schema")
    (built["project"] / "tools" / "migrate.ts").write_text("process.exit(0);\n", encoding="utf-8")  # ignored: outside the scope
    started = time.monotonic()
    result = built["tools"].verify_fix(built["run"]["run_id"])
    assert result["status"] == "ok" and result["verified"] is True, result
    assert gate(result) == dict.fromkeys(GATES, True) and result["containers_left"] == 0
    assert len(result["regression"]) == 3 and all("passed, PASS, database matched" in line for line in result["regression"])
    assert result["changed_files"].startswith("3 [project]: ") and "migrations/0001_one_redemption_per_code.sql" in result["changed_files"]
    assert "tools/migrate.ts" in result["not_deployed"]
    assert "owner role the prestart step runs as" in result["hooks"] and "separate container" in result["hooks"]
    # A unique constraint adds no trigger, rule, view, policy or SECURITY DEFINER function: no flag.
    assert result["review_required"] is False and "schema_objects" not in result and "review" not in result
    with capsys.disabled():
        print(f"\nverify_fix, schema fix through the prestart, build and 4 stack runs: {time.monotonic() - started:.0f} s ({result['build']})")


@real_build
def test_a_candidate_migration_that_fails_is_a_failed_run_and_never_verified(built) -> None:
    overlay(built["project"], DEMO / "reference_fix_schema")
    (built["project"] / "migrations" / "0001_one_redemption_per_code.sql").write_text("ALTER TABLE public.nope ADD COLUMN x int;\n", encoding="utf-8")
    result = built["tools"].verify_fix(built["run"]["run_id"])
    assert result["status"] == "error" and result["verified"] is False and result["error_class"] == "sandbox_failed", result
    assert "target_start_failed" in result["message"] and "[project] migrate: 0001_one_redemption_per_code failed; rolled back (SQLSTATE 42P01)" in result["message"]
    assert result["containers_left"] == 0
    overlay(built["project"], None)


@real_build
def test_the_reference_fix_is_verified_through_a_real_build(built, capsys: pytest.CaptureFixture) -> None:
    overlay(built["project"], DEMO / "reference_fix")
    started = time.monotonic()
    result = built["tools"].verify_fix(built["run"]["run_id"])
    assert result["status"] == "ok" and result["verified"] is True, result
    assert gate(result) == dict.fromkeys(GATES, True) and result["containers_left"] == 0
    assert len(result["regression"]) == 3 and all("passed, PASS, database matched" in line for line in result["regression"])
    assert result["changed_files"] == "1 [project]: src/lib/server/redeem.ts" and "separate container" in result["hooks"]
    assert result["review_required"] is False and "schema_objects" not in result
    with capsys.disabled():
        print(f"\nverify_fix, reference fix, build and 4 stack runs: {time.monotonic() - started:.0f} s ({result['build']})")


FALSE_FIXES = {
    "rejects_everyone": fix_agent.GATE_FIX_VERIFIED,
    "skips_audit": fix_agent.GATE_FIX_VERIFIED,
    "wipes_codes": fix_agent.GATE_FIX_VERIFIED,
    "drops_revocation_check": fix_agent.GATE_REGRESSION,
    # The race is fixed by a real constraint; the same migration adds a rule that swallows every audit row.
    "migration_mutes_audit": fix_agent.GATE_FIX_VERIFIED,
}


@real_build
@pytest.mark.parametrize("name", list(FALSE_FIXES))
def test_each_false_fix_is_rejected_by_the_row_the_demo_names(built, name: str, capsys: pytest.CaptureFixture) -> None:
    overlay(built["project"], DEMO / "false_fixes" / name)
    result = built["tools"].verify_fix(built["run"]["run_id"])
    assert result["status"] == "ok" and result["verified"] is False and result["run_id"], result
    failed = [row for row, passed in gate(result).items() if not passed]
    assert failed == [FALSE_FIXES[name]], result
    if name == "drops_revocation_check":
        assert any(line.startswith("revoked-code-refused: FAILED") for line in result["regression"])
    if name == "migration_mutes_audit":
        # The gate rejected it on its own. The flag names the rule the migration added, as well.
        assert result["review_required"] is True and result["schema_objects"] == ["[project] added: rule audit_log_muted on public.audit_log"]
        assert result["review"].startswith("not verified, and the fix added database objects")
        assert "migrations/0001_one_redemption_per_code.sql" in result["review_files"]
    else:
        assert result["review_required"] is False and "schema_objects" not in result
    assert result["containers_left"] == 0 and len(json.dumps(result, indent=1).splitlines()) <= 60
    with capsys.disabled():
        print(f"\n{name}: rejected by {failed[0]}")


@real_build
def test_a_fix_whose_migration_adds_a_trigger_is_judged_by_the_gate_and_flagged_for_review(built, capsys: pytest.CaptureFixture) -> None:
    """flagged_fix is the schema fix plus a trigger that drops audit rows the scenarios never write.
    The flag is not a gate row: `verified` is whatever the four rows say, and here they all pass."""
    overlay(built["project"], DEMO / "flagged_fix")
    result = built["tools"].verify_fix(built["run"]["run_id"])
    assert result["status"] == "ok" and result["run_id"], result
    assert result["verified"] is all(gate(result).values()), "the gate decides, with or without the flag"
    assert result["verified"] is True and gate(result) == dict.fromkeys(GATES, True), result
    assert result["review_required"] is True
    # The trigger and the function it calls, both from the migration, so neither appeared during the run.
    assert result["schema_objects"] == [
        "[project] added: function public.audit_log_only_redeems()",
        "[project] added: trigger audit_log_only_redeems on public.audit_log",
    ]
    assert built["tools"].get_run(result["run_id"], "schema_objects")["items"] == result["schema_objects"]
    assert result["review"] == (
        "verified by the gate, but the fix added database objects that can change what the state check reads; "
        "read them before trusting the result."
    )
    assert result["review_files"] == "1 [project]: migrations/0001_one_redemption_per_code.sql"
    assert result["containers_left"] == 0 and len(json.dumps(result, indent=1).splitlines()) <= 60
    summary = built["tools"].get_run(result["run_id"])
    assert summary["fix_gate"].startswith("verified=True against ") and "review required" in summary["fix_gate"]
    with capsys.disabled():
        print(f"\nflagged_fix: verified={result['verified']}, review_required={result['review_required']}: {result['schema_objects'][1]}")


@real_build
def test_edits_outside_the_scope_fail_code_changed_and_start_no_build_container(built) -> None:
    overlay(built["project"], DEMO / "false_fixes" / "outside_scope_only")
    before = set(labelled("container"))
    started = time.monotonic()
    result = built["tools"].verify_fix(built["run"]["run_id"])
    assert result["verified"] is False and result["run_id"] is None, result
    assert [row for row, passed in gate(result).items() if not passed] == GATES
    assert "(identical)" in next(row["evidence"] for row in result["gate"] if row["gate"] == fix_agent.GATE_CODE)
    for name in ("faultforge.yaml", "stack.yaml", "package.json"):
        assert name in result["fix_next"][0]
    assert time.monotonic() - started < 10 and set(labelled("container")) <= before


@real_build
def test_a_syntax_error_is_a_clean_build_failure(built) -> None:
    overlay(built["project"], None)
    (built["project"] / "src" / "lib" / "server" / "redeem.ts").write_text("export async function redeem( {\n", encoding="utf-8")
    result = built["tools"].verify_fix(built["run"]["run_id"])
    assert result["status"] == "ok" and result["verified"] is False and result["run_id"] is None, result
    assert result["build_failed"] == "build_failed" and result["build_log"] and all(line.startswith("[project] ") for line in result["build_log"])
    assert not any(gate(result).values()) and result["containers_left"] == 0
    overlay(built["project"], None)


HOSTILE = r"""
// Runs when the build imports this module. It tries to get out, then fails the build with what it
// found, so the report is in the build's own output.
import { execFileSync } from 'node:child_process';
import { existsSync, writeFileSync } from 'node:fs';
import net from 'node:net';

function attempt(action: () => void): string {
	try { action(); return 'DONE'; } catch (error) { return String((error as NodeJS.ErrnoException).code ?? 'failed'); }
}
function connect(host: string, port: number): Promise<string> {
	return new Promise((resolve) => {
		const socket = net.connect({ host, port, timeout: 3000 });
		socket.on('connect', () => { socket.destroy(); resolve('CONNECTED'); });
		socket.on('error', (error: NodeJS.ErrnoException) => resolve(String(error.code)));
		socket.on('timeout', () => { socket.destroy(); resolve('timeout'); });
	});
}
const report = {
	uid: process.getuid?.(),
	network: await connect('1.1.1.1', 443),
	registry: await connect('104.16.0.35', 443),
	etc: attempt(() => writeFileSync('/etc/ff-hostile', 'x')),
	src: attempt(() => writeFileSync('/src/ff-hostile', 'x')),
	deps: attempt(() => writeFileSync('/deps/node_modules/ff-hostile', 'x')),
	pkg: attempt(() => writeFileSync('/app/node_modules/vite/package.json', '{}')),
	sock: existsSync('/var/run/docker.sock'),
	id: attempt(() => execFileSync('id', ['-u'])),
	env: Object.keys(process.env).filter((name) => /KEY|SECRET|TOKEN|PASSWORD|DATABASE|VULTR|PROXY/i.test(name)),
};
throw new Error('FF_HOSTILE_REPORT ' + JSON.stringify(report) + ' FF_END');
"""


def volume_listing(volume: str) -> str:
    """A hash of every file's name, size and time in the cache volume, read by a container with no network."""
    result = subprocess.run([
        "docker", "run", "--rm", "--pull", "never", "--network", "none", "--read-only", "--cap-drop", "ALL",
        "--mount", f"type=volume,src={volume},dst=/deps,readonly", "--entrypoint", "sh", STACK.build.build_image,
        "-c", "cd /deps && find . -type f -exec stat -c '%n %s %Y' {} + | sort | sha256sum",
    ], capture_output=True, check=True, text=True)
    return result.stdout.split()[0]


@real_build
def test_code_that_runs_during_a_build_is_contained(built, tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    source = tmp_path / "hostile"
    shutil.copytree(DEMO, source, ignore=shutil.ignore_patterns("node_modules", "build", ".svelte-kit"))
    config = source / "vite.config.ts"
    config.write_text(config.read_text(encoding="utf-8") + HOSTILE, encoding="utf-8")
    keys = node_build.cache_keys(TEST_STATE)
    assert keys, "the fixture built from this cache"
    volume = node_build.volume_name(keys[0])
    before = volume_listing(volume)
    result = node_build.build_stack_image(source, STACK.build, TEST_STATE, allow_fetch=False)
    assert result.image_id is None and result.failure_reason == "build_failed" and result.install == "cache hit", result
    found = re.search(r"FF_HOSTILE_REPORT (\{.*?\}) FF_END", result.log_tail or "", re.DOTALL)
    assert found, result.log_tail
    report = json.loads(found[1])
    with capsys.disabled():
        print(f"\nhostile build: {report}")
    assert report["uid"] == 1000 and report["network"] != "CONNECTED" and report["registry"] != "CONNECTED"
    assert report["etc"] != "DONE" and report["src"] != "DONE" and report["deps"] != "DONE" and report["pkg"] != "DONE"
    assert report["sock"] is False and report["env"] == []
    assert volume_listing(volume) == before
    assert result.cache_key == keys[0]


# The one test that uses the network


@npm_network
def test_a_cold_fetch_reaches_the_registry_through_the_proxy_and_nothing_else(capsys: pytest.CaptureFixture) -> None:
    policy = node_build.POLICY
    build = STACK.build
    image = node_build.toolchain_ids(build)["build"]
    # First, what a container on the install network can reach on its own: nothing.
    sandbox_id = f"sb-{secrets.token_hex(6)}"
    expires = int(time.time()) + 300
    try:
        for argv in node_build.network_argv(sandbox_id, expires).values():
            assert _docker(policy, argv).returncode == 0
        described = json.loads(_docker(policy, ["network", "inspect", node_build.install_network(sandbox_id)]).stdout)[0]
        assert described["Internal"] is True and all("Gateway" not in item for item in described["IPAM"]["Config"])
        assert _docker(policy, node_build.proxy_argv(sandbox_id, expires)).returncode == 0
        deadline = time.monotonic() + 15
        while b'"listening"' not in _docker(policy, ["logs", node_build.proxy_name(sandbox_id)]).stdout:
            assert time.monotonic() < deadline
            time.sleep(0.25)
        assert _docker(policy, ["network", "connect", node_build.egress_network(sandbox_id), node_build.proxy_name(sandbox_id)]).returncode == 0
        gateway = json.loads(_docker(policy, ["network", "inspect", node_build.egress_network(sandbox_id)]).stdout)[0]["IPAM"]["Config"][0]["Gateway"]
        probe = (
            "const net=require('net');const tries=[['1.1.1.1',443],[process.argv[1],443],[process.argv[1],3128],['example.com',443],['registry.npmjs.org',443]];"
            "Promise.all(tries.map(([host,port])=>new Promise(done=>{const s=net.connect({host,port,timeout:4000});"
            "s.on('connect',()=>{s.destroy();done('CONNECTED')}).on('error',e=>done(e.code)).on('timeout',()=>{s.destroy();done('timeout')})})))"
            ".then(results=>console.log(JSON.stringify(results)))"
        )
        # The fetch container's own flags and network, with a probe in place of pnpm.
        argv = [
            *node_build._sealed(f"ff-{sandbox_id}-install", sandbox_id, "install", expires, 512, 1, 128, 1),
            "--network", node_build.install_network(sandbox_id), "--user", node_build.BUILD_USER,
            "--entrypoint", "node", image, "-e", probe, gateway,
        ]
        argv[1] = "--rm"  # in place of --detach: the output is the result
        result = _docker(policy, argv, 60.0)
        assert result.returncode == 0, result.stderr
        outcomes = json.loads(result.stdout)
        assert "CONNECTED" not in outcomes, outcomes
    finally:
        _remove_sandbox(policy, sandbox_id)
        for network in (node_build.install_network(sandbox_id), node_build.egress_network(sandbox_id)):
            _docker(policy, ["network", "rm", network])
    assert labelled("container", sandbox_id) == [] and labelled("network", sandbox_id) == []

    # Then the real thing, into the kept state, so the build tests find the cache warm.
    linted = node_build.lint(DEMO, build)
    key = node_build.cache_key(image, linted, TEST_STATE)
    if node_build.cache_ready(TEST_STATE, key):
        assert node_build.remove_cache_volume(policy, node_build.volume_name(key))
        node_build._index_path(TEST_STATE, key).unlink()
    networks_before = set(labelled("network"))
    fetched = node_build.build_stack_image(DEMO, build, TEST_STATE, allow_fetch=True)
    try:
        assert fetched.image_id is not None and fetched.install == "fetched", fetched
        assert len(fetched.proxy) == 1 and fetched.proxy[0].startswith("registry.npmjs.org:443: ") and "refused" not in " ".join(fetched.proxy)
        assert int(fetched.proxy[0].split(" connections")[0].rsplit(" ", 1)[1]) > 0
        assert node_build.cache_ready(TEST_STATE, key) and fetched.cache_key == key
        assert set(labelled("network")) <= networks_before
        with capsys.disabled():
            print(f"\ncold build of the demo: {fetched.line()}; proxy: {fetched.proxy[0]}")
        # The same lockfile again never fetches.
        again = node_build.build_stack_image(DEMO, build, TEST_STATE, allow_fetch=False)
        assert again.image_id is not None and again.install == "cache hit" and again.source_sha256 == fetched.source_sha256
        node_build.remove_images([again.image_id])
    finally:
        if fetched.image_id:
            node_build.remove_images([fetched.image_id])


def test_the_demo_names_its_false_fixes_and_ships_a_lockfile_of_the_real_shape() -> None:
    table = (DEMO / "false_fixes" / "README.md").read_text(encoding="utf-8")
    for name, row in {**FALSE_FIXES, "outside_scope_only": fix_agent.GATE_CODE}.items():
        assert (DEMO / "false_fixes" / name).is_dir() and re.search(rf"`{name}`.*`{row}`", table)
    assert node_build.lint_problems(DEMO, STACK.build) == []
    # Both reference fixes and the migration false fix stay inside the fix scope, and the runner stays outside it.
    for name in ("reference_fix", "reference_fix_schema", "false_fixes/migration_mutes_audit", "flagged_fix"):
        files = node_build._regular_files(DEMO / name)
        assert files and all(node_build.in_scope(item, STACK.build.fix_scope) for item in files), name
    # The flagged fix is the schema fix with a trigger added, and the demo says what happens to it.
    flagged = (DEMO / "flagged_fix" / "migrations" / "0001_one_redemption_per_code.sql").read_text(encoding="utf-8")
    assert "ADD CONSTRAINT redemptions_code_id_key UNIQUE (code_id)" in flagged and "CREATE TRIGGER audit_log_only_redeems" in flagged
    assert "`flagged_fix/`" in (DEMO / "README.md").read_text(encoding="utf-8")
    assert (DEMO / "tools" / "migrate.ts").is_file() and not node_build.in_scope("tools/migrate.ts", STACK.build.fix_scope)
    assert not (DEMO / "src" / "hooks.server.ts").exists(), "the app no longer applies its own migrations"
    assert dataclasses.is_dataclass(node_build.StackBuild) and STACK.app_user == node_build.BUILD_USER
