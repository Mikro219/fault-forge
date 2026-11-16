"""M11: the redeem-code demo in real containers.

The default tests need the images scripts/build_redeem_demo.sh makes and skip without them. They
run the prebuilt buggy and fixed images, and check what M11 changed for every stack: postgres asks
for a password on every TCP login, admin sessions pin their search_path, and there is no redis.

FF_TEST_STACK_BUILD=1 adds the real builder through the MCP tools: a baseline from source, the
reference fix, every false fix and a hostile build. They need a warm install cache and skip on a
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
from faultforge.stack import ADMIN_ROLE, HOOKS_IMAGE, STACK_POLICY, StackSession, StackSpec, load_stack, psql_state_argv, run_stack, setup_role_sql

ROOT = Path(__file__).resolve().parents[1]
DEMO = ROOT / "demo" / "redeem-code"
SPORTSDECK = ROOT / "demo" / "sportsdeck"
CONFIG = DEMO / "faultforge.yaml"
STACK = load_stack(DEMO / "stack.yaml")
BUGGY, FIXED = "faultforge-redeem-code:buggy", "faultforge-redeem-code:fixed"
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
    not all(has_image(image) for image in (BUGGY, FIXED, HOOKS_IMAGE, STACK.postgres_image)),
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
    # No redis: postgres and the hooks helper are the only trusted services.
    assert sorted(service.user for service in facts.services) == ["65534:65534", "70:70"]
    assert facts.target.user == "1000:1000" and facts.target.limits_applied and all(service.limits_applied for service in facts.services)
    assert facts.target.network_mode.startswith("container:") and "FF_HOOKS_TOKEN" not in facts.target.env_names
    # The runner joins postgres's namespace too, and it alone holds the hooks token.
    assert facts.runner.limits_applied and facts.runner.network_mode == facts.target.network_mode
    assert "FF_HOOKS_TOKEN" in facts.runner.env_names
    assert "REDIS_URL" not in facts.target.env_names and "POSTGRES_PASSWORD" not in facts.target.env_names


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
        results.append({"ok": False, "error": type(error).__name__})
print(json.dumps(results))
"""


class Postgres:
    """A stack's postgres alone, started exactly as StackSession starts it, with the app's role set up."""

    def __init__(self, stack, config: Path, tmp_path: Path):
        self.stack = stack
        self.session = StackSession(StackSpec("unused", stack, config), tmp_path, "unused", RunOptions(), STACK_POLICY, time.monotonic())
        self.app_password, self.admin_password = secrets.token_hex(16), secrets.token_hex(16)
        self.expires = int(time.time()) + 300

    def __enter__(self) -> "Postgres":
        session = self.session
        argv = session.argv(self.app_password, self.expires, self.admin_password)
        try:
            assert _docker(STACK_POLICY, argv["postgres"]).returncode == 0
            session._await(["exec", session.postgres_name, "pg_isready", "-h", "127.0.0.1", "-U", ADMIN_ROLE], "postgres")
            setup = self.psql(setup_role_sql(self.stack, self.app_password))
            assert setup.returncode == 0, setup.stderr
        except BaseException:
            _remove_sandbox(STACK_POLICY, session.sandbox_id)
            raise
        return self

    def __exit__(self, *_: object) -> None:
        assert _remove_sandbox(STACK_POLICY, self.session.sandbox_id) == 0

    def psql(self, sql: str) -> subprocess.CompletedProcess:
        """The host's own route: docker exec into the postgres container, over its unix socket."""
        argv = psql_state_argv(self.session.postgres_name, self.stack, {"x": "SELECT 1"})
        return _docker(STACK_POLICY, [*argv[:-1], sql])

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
        allowed = [
            {"user": stack.database_user, "host": "127.0.0.1", "database": database, "password": postgres.app_password},
            # The hooks helper's own login: the admin role, over TCP, with its password (SCRAM, through pg8000).
            {"user": ADMIN_ROLE, "host": "127.0.0.1", "database": database, "password": postgres.admin_password,
             "sql": ["SET search_path = pg_catalog, public", "SELECT rolsuper FROM pg_roles WHERE rolname = current_user"]},
            {"user": stack.database_user, "host": "127.0.0.1", "database": database, "password": postgres.app_password,
             "sql": ["SELECT rolsuper FROM pg_roles WHERE rolname = current_user"]},
        ]
        results = postgres.probe([*refused, *allowed])
        assert [item["ok"] for item in results[: len(refused)]] == [False] * len(refused), results
        assert [item["ok"] for item in results[len(refused):]] == [True, True, True], results
        assert results[-2]["rows"] == [[True]] and results[-1]["rows"] == [[False]]
        # The host still reads as the administrator, through the unix socket, with no password.
        answer = postgres.psql("SELECT current_user")
        assert answer.returncode == 0 and answer.stdout.decode().strip() == ADMIN_ROLE
        method = postgres.psql("SELECT string_agg(DISTINCT auth_method, ',') FROM pg_hba_file_rules WHERE type = 'host'")
        assert method.stdout.decode().strip() == "scram-sha-256"


@postgres_images
def test_an_app_defined_count_cant_stand_in_for_the_real_one(tmp_path: Path) -> None:
    """The app owns its database and schema. It defines public.count(*) and makes public come first
    in its database's default search_path. Admin sessions pin theirs, so COUNT(*) is still pg_catalog's."""
    with Postgres(STACK, CONFIG, tmp_path) as postgres:
        app = {"user": STACK.database_user, "host": "127.0.0.1", "database": STACK.database_name, "password": postgres.app_password}
        planted = postgres.probe([{**app, "sql": [
            "CREATE TABLE public.things (id int)",
            "INSERT INTO public.things VALUES (1), (2), (3)",
            "CREATE FUNCTION public.always(bigint) RETURNS bigint LANGUAGE sql AS 'SELECT 999::bigint'",
            "CREATE AGGREGATE public.count(*) (SFUNC = public.always, STYPE = bigint, INITCOND = '999')",
            f"ALTER DATABASE {STACK.database_name} SET search_path = public, pg_catalog",
        ]}])
        assert planted[0]["ok"], planted
        query = "SELECT COUNT(*) FROM public.things"
        admin = {"user": ADMIN_ROLE, "host": "127.0.0.1", "database": STACK.database_name, "password": postgres.admin_password}
        fooled, helper = postgres.probe([
            {**admin, "sql": [query]},                                           # the control: an unpinned admin session is fooled
            {**admin, "sql": ["SET search_path = pg_catalog, public", query]},   # what the hooks helper runs
        ])
        assert fooled["rows"] == [[999]] and helper["rows"] == [[3]]
        host = _docker(STACK_POLICY, psql_state_argv(postgres.session.postgres_name, STACK, {"things": query}))
        assert host.returncode == 0 and json.loads(host.stdout) == {"things": 3}


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
    with capsys.disabled():
        print(f"\nrun_scenario from source, warm cache: {built['seconds']:.0f} s ({run['source']['build']})")


@real_build
def test_the_reference_fix_is_verified_through_a_real_build(built, capsys: pytest.CaptureFixture) -> None:
    overlay(built["project"], DEMO / "reference_fix")
    started = time.monotonic()
    result = built["tools"].verify_fix(built["run"]["run_id"])
    assert result["status"] == "ok" and result["verified"] is True, result
    assert gate(result) == dict.fromkeys(GATES, True) and result["containers_left"] == 0
    assert len(result["regression"]) == 3 and all("passed, PASS, database matched" in line for line in result["regression"])
    assert result["changed_files"] == "1 [project]: src/lib/server/redeem.ts" and "separate container" in result["hooks"]
    with capsys.disabled():
        print(f"\nverify_fix, reference fix, build and 4 stack runs: {time.monotonic() - started:.0f} s ({result['build']})")


FALSE_FIXES = {
    "rejects_everyone": fix_agent.GATE_FIX_VERIFIED,
    "skips_audit": fix_agent.GATE_FIX_VERIFIED,
    "wipes_codes": fix_agent.GATE_FIX_VERIFIED,
    "drops_revocation_check": fix_agent.GATE_REGRESSION,
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
    assert result["containers_left"] == 0
    with capsys.disabled():
        print(f"\n{name}: rejected by {failed[0]}")


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
    assert dataclasses.is_dataclass(node_build.StackBuild) and STACK.app_user == node_build.BUILD_USER
