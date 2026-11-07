"""A multi-service target: unit tests, then real containers if the app's images are built."""

import base64
import hashlib
import hmac
import io
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tarfile
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from faultforge import config as config_module, fix_agent, stack_hooks
from faultforge.errors import FaultForgeError
from faultforge.fix_agent import ApiKey, ModelPrice, OutputRejected, TokenBudget
from faultforge.lifecycle import RunOptions
from faultforge.models import Comparison, InvariantComparison
from faultforge.sandbox import ContainmentFacts, SandboxResult, StateVerification
from faultforge.stack import HOOKS_IMAGE, StackSession, StackSpec, STACK_POLICY, load_stack, psql_state_argv, run_stack, setup_role_sql
from faultforge.stack_fix import (
    BuildResult,
    build_argv,
    build_candidate,
    build_messages,
    check_typescript_policy,
    UnsafeOutput,
    extract_output,
    extract_typescript,
    load_fix_config,
    read_image_file,
    redact,
    remove_candidate_images,
    run_stack_fix_loop,
)

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import model_trial  # noqa: E402

DEMO = ROOT / "demo" / "sportsdeck"
CONFIG = DEMO / "faultforge.yaml"
STACK = load_stack(DEMO / "stack.yaml")
BUGGY, FIXED = "faultforge-sportsdeck:buggy", "faultforge-sportsdeck:fixed"
RACE = "refresh-token-race"


def has_image(image: str) -> bool:
    if shutil.which("docker") is None:
        return False
    return subprocess.run(["docker", "image", "inspect", image], capture_output=True, check=False).returncode == 0


stack_images = pytest.mark.skipif(
    not all(has_image(image) for image in (BUGGY, FIXED, HOOKS_IMAGE)), reason="build demo/sportsdeck first",
)


def session(tmp_path: Path) -> StackSession:
    return StackSession(StackSpec(BUGGY, STACK, CONFIG), tmp_path, RACE, RunOptions(), STACK_POLICY, 0.0)


def test_the_definition_parses_and_has_a_verifiable_fixed_token() -> None:
    config = config_module.parse_config(CONFIG.read_text(encoding="utf-8"))
    assert config.target.hooks_origin == "http://127.0.0.1:8001"
    token = config_module.find_scenario(config, RACE).request.json_body["refreshToken"]
    head, payload, signature = token.split(".")
    digest = hmac.new(STACK.env["JWT_REFRESH_SECRET"].encode(), f"{head}.{payload}".encode(), hashlib.sha256).digest()
    assert base64.urlsafe_b64encode(digest).rstrip(b"=").decode() == signature
    assert token in " ".join(STACK.reset_sql)
    assert json.loads(base64.urlsafe_b64decode(payload + "=" * 4))["userId"] == 900001


def test_the_definition_holds_only_test_values() -> None:
    assert all("sandbox-only" in value or value == "production" for value in STACK.env.values())


def test_every_container_is_locked_down_and_only_postgres_owns_the_network(tmp_path: Path) -> None:
    built = session(tmp_path).argv("pw", 1)
    for name, args in built.items():
        assert "--read-only" in args and args[args.index("--cap-drop") + 1] == "ALL"
        assert args[args.index("--security-opt") + 1] == "no-new-privileges"
        assert "--memory" in args and "--pids-limit" in args
        assert args[args.index("--ipc") + 1] == "none"
        assert "--pull" in args and args[args.index("--pull") + 1] == "never"
        assert "--user" in args and not args[args.index("--user") + 1].startswith("0")
        assert not any("docker.sock" in item for item in args), name
        network = args[args.index("--network") + 1]
        assert network == ("none" if name == "postgres" else f"container:{session(tmp_path).postgres_name}") or network.startswith("container:ff-")


def test_only_the_app_gets_app_settings_and_no_real_secret_names(tmp_path: Path) -> None:
    built = session(tmp_path).argv("pw", 1)
    assert not any("JWT" in item for item in built["postgres"] + built["redis"] + built["hooks"])
    for forbidden in ("VULTR", "FOOTBALL_DATA_API_KEY", "HUGGINGFACE", "GITHUB_CLIENT_SECRET", "SMTP"):
        assert not any(forbidden in item for args in built.values() for item in args)


def test_psql_reads_every_field_in_one_statement() -> None:
    argv = psql_state_argv("c", STACK, STACK.state_sql)
    assert argv[:2] == ["exec", "c"] and sum("json_build_object" in item for item in argv) == 1
    assert "'live_tokens', (SELECT COUNT(*)" in argv[-1]


def test_hooks_state_has_what_the_core_requires() -> None:
    body = stack_hooks.state_body({"live_tokens": 1}, "abc", "i", "t")
    assert body["live_tokens"] == 1 and body["in_flight"] == 0 and body["stale_source"] is False
    assert body["build"]["build_id"] == "abc"
    assert stack_hooks.database_state(lambda sql: [[3]], {"live_tokens": "x"}) == {"live_tokens": 3}


def test_a_hook_origin_must_be_loopback(tmp_path: Path) -> None:
    text = CONFIG.read_text(encoding="utf-8").replace("http://127.0.0.1:8001", "http://169.254.169.254:8001")
    with pytest.raises(FaultForgeError) as caught:
        config_module.parse_config(text)
    assert "169.254.169.254" in str(caught.value) and "port" not in str(caught.value)


@stack_images
def test_real_race_fix_and_regression_in_a_sealed_stack(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    project = tmp_path / "project"
    buggy = run_stack(StackSpec(BUGGY, STACK, CONFIG), project, RACE)
    assert buggy.status == "completed", buggy.failure_detail
    assert buggy.report.verdict == "FAIL", buggy.report.verdict_reason
    assert max(trial.client.accepted for trial in buggy.report.trials) > 1
    assert buggy.containment.state_verification.status == "matched"
    assert buggy.containment.containers_left == 0

    fixed = run_stack(StackSpec(FIXED, STACK, CONFIG), project, RACE, RunOptions(compare_to=buggy.report.run_id))
    assert fixed.status == "completed", fixed.failure_detail
    assert fixed.report.verdict == "PASS", fixed.report.verdict_reason
    assert fix_agent.gate_failures(fixed, buggy.deployed_code_sha256) == ()
    assert all(trial.client.accepted == 1 for trial in fixed.report.trials if trial.status == "completed")

    other = run_stack(StackSpec(FIXED, STACK, CONFIG), project, "garbage-tokens-rejected")
    assert other.status == "completed", other.failure_detail
    assert other.report.verdict == "PASS", other.report.verdict_reason
    assert other.containment.state_verification.status == "matched"

    for result in (buggy, fixed, other):
        facts = result.containment
        assert facts.target.read_only_rootfs and "ALL" in facts.target.cap_drop and facts.target.limits_applied
        assert facts.target.network_mode.startswith("container:")
        assert {item.network_mode.split(":")[0] for item in facts.services} == {"none", "container"}
        assert facts.containers_left == 0
    with capsys.disabled():
        accepted = [(trial.kind, trial.client.accepted) for trial in buggy.report.trials]
        print(f"\n[stack] buggy {accepted} wall={buggy.containment.wall_s} s; fixed wall={fixed.containment.wall_s} s; other wall={other.containment.wall_s} s")


# The fix agent, for TypeScript


FIX = load_fix_config(DEMO / "fix.yaml")
GOOD_TS = """import { NextRequest } from "next/server";
import { prisma } from "@/lib/prisma";
import { errorResponse } from "@/lib/utils";

export async function POST(request: NextRequest) {
  return errorResponse("x", 401);
}
"""


def test_a_clean_file_passes_the_code_rules() -> None:
    assert check_typescript_policy(GOOD_TS, FIX) == ()


@pytest.mark.parametrize("line", [
    'import { exec } from "child_process";', 'import fs from "fs";', 'import "side-effect";', 'const m = await import("fs");',
    'const m = require("fs");', "eval(code);", 'const f = new Function("x");', "const k = process.env.JWT_SECRET;",
    'await fetch("http://example.com");', "globalThis.x = 1;",
])
def test_the_code_rules_reject_what_a_fix_never_needs(line: str) -> None:
    assert check_typescript_policy(GOOD_TS + line + "\n", FIX)


def test_the_required_export_is_required() -> None:
    assert check_typescript_policy('import { NextRequest } from "next/server";\n', FIX)


def test_extraction_needs_one_typescript_block() -> None:
    assert extract_typescript("```typescript\nconst a = 1;\n```") == "const a = 1;\n"
    with pytest.raises(OutputRejected) as caught:
        extract_typescript("```python\na = 1\n```")
    assert caught.value.reason == "not_typescript"
    with pytest.raises(OutputRejected) as caught:
        extract_typescript("```ts\na\n```\n```ts\nb\n```")
    assert caught.value.reason == "multiple_blocks"


def test_the_build_is_sealed(tmp_path: Path) -> None:
    source = tmp_path / "candidate.ts"
    args = build_argv("n", "sb-x", FIX, source, 1)
    assert args[args.index("--network") + 1] == "none"
    assert args[args.index("--cap-drop") + 1] == "ALL" and args[args.index("--security-opt") + 1] == "no-new-privileges"
    assert "--memory" in args and "--pids-limit" in args and "--pull" in args
    joined = " ".join(args)
    assert f"dst=/app/{FIX.editable},readonly" in joined
    for forbidden in ("JWT", "DATABASE_URL", "VULTR", "docker.sock"):
        assert forbidden not in joined


def test_the_prompt_holds_the_file_rules_and_no_reference_fix() -> None:
    sources = {FIX.editable: "ROUTE", **{name: "CONTEXT" for name in FIX.context}}
    report = model_trial.synthetic_report("run_20260101_000000_0001")
    system, user = build_messages(FIX, sources, "RULES", report, [], frozenset())
    assert "RULES" in system["content"] and FIX.editable in system["content"]
    assert "ROUTE" in user["content"] and "CONTEXT" in user["content"]
    assert "deleteMany" not in system["content"] + user["content"]


def _loop(tmp_path, build, sandbox, transport, **overrides):
    baseline = model_trial.synthetic_report("run_20260101_000000_0001")
    args = dict(
        model="glm-5.3", api_key=ApiKey("canary-key"), budget=TokenBudget(1.0, {"glm-5.3": ModelPrice(1.0, 1.0)}), config=FIX,
        sources={FIX.editable: "R", **{name: "C" for name in FIX.context}}, rules="X", baseline=baseline,
        baseline_code_sha256="a" * 64, stack=STACK, config_path=CONFIG, project_dir=tmp_path, scenario_id=RACE,
        regression_scenarios=("garbage-tokens-rejected",), transport=transport, build=build, run_sandboxed=sandbox, attempts=2,
    )
    return run_stack_fix_loop(**{**args, **overrides})


def _reply(code: str = GOOD_TS):
    def respond(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "choices": [{"finish_reason": "stop", "message": {"content": f"```typescript\n{code}```"}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 10},
        })
    return httpx.MockTransport(respond)


def test_a_build_failure_is_a_failed_attempt_that_goes_back_to_the_model(tmp_path: Path) -> None:
    seen = []

    def respond(request: httpx.Request) -> httpx.Response:
        seen.append(request.content.decode())
        return httpx.Response(200, json={
            "choices": [{"finish_reason": "stop", "message": {"content": f"```typescript\n{GOOD_TS}```"}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 10},
        })

    def build(_code, _config):
        return BuildResult(None, "build_failed", "next build failed. Type error: TS1234")

    def never(*_a):
        raise AssertionError("a failed build must never run")

    result = _loop(tmp_path, build, never, httpx.MockTransport(respond))
    assert result.success is False and len(result.attempts) == 2
    assert result.attempts[0].failure_reason == "build_failed"
    assert "TS1234" in seen[1] and "TS1234" not in seen[0]


def test_code_that_breaks_a_rule_is_never_built(tmp_path: Path) -> None:
    def build(_code, _config):
        raise AssertionError("must not build")

    result = _loop(tmp_path, build, None, _reply(GOOD_TS + 'const k = process.env.X;\n'), attempts=1)
    assert result.attempts[0].failure_reason == "policy_violation" and result.success is False


def test_a_real_fix_loop_needs_the_gate_and_every_regression(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("faultforge.stack_fix.remove_candidate_images", lambda images: None)
    passing = model_trial.synthetic_report("run_20260101_000000_0003", "PASS")
    comparison = Comparison(
        baseline_run_id="run_20260101_000000_0001", status="COMPARABLE", fix_verified=True,
        invariants=[InvariantComparison(name="A refresh token is redeemed at most once", result="FIXED")],
    )
    verified = model_trial.synthetic_report("run_20260101_000000_0002", "PASS", comparison)
    calls = []

    def outcome(report, sha="b" * 64):
        return SandboxResult(
            status="completed", report=report, deployed_code_sha256=sha,
            containment=ContainmentFacts(wall_s=1, wall_limit_s=100, state_verification=StateVerification(status="matched")),
        )

    def sandbox(spec, _project, scenario, _options):
        calls.append((spec.app_image, scenario))
        return outcome(verified if scenario == RACE else passing)

    build = lambda _code, _config: BuildResult("faultforge-candidate:x")
    result = _loop(tmp_path, build, sandbox, _reply(), attempts=1)
    assert result.success is True and result.stop_reason == "fix_verified"
    assert calls == [("faultforge-candidate:x", RACE), ("faultforge-candidate:x", "garbage-tokens-rejected")]

    def broken(spec, _project, scenario, _options):
        return outcome(verified if scenario == RACE else model_trial.synthetic_report("run_20260101_000000_0004"))

    failed = _loop(tmp_path, build, broken, _reply(), attempts=1)
    assert failed.success is False and failed.attempts[0].failure_reason == "regression_failed"

    # The image can't be the baseline's.
    same = _loop(tmp_path, build, lambda *_: outcome(verified, "a" * 64), _reply(), attempts=1)
    assert same.success is False and same.attempts[0].failure_reason == "code_unchanged"


full_build = pytest.mark.skipif(
    not (os.environ.get("FF_TEST_STACK_BUILD") == "1" and has_image(FIX.builder_image)),
    reason="set FF_TEST_STACK_BUILD=1: a real next build takes minutes",
)


@full_build
def test_a_sealed_build_makes_a_working_image_and_a_broken_file_fails_cleanly(capsys: pytest.CaptureFixture) -> None:
    source = read_image_file(FIX.builder_image, f"/app/{FIX.editable}")
    broken = build_candidate(source + "\nconst x: number = 'not a number';\n", FIX)
    assert broken.image is None and broken.failure_reason == "build_failed" and "number" in broken.failure_detail
    good = build_candidate(source + "\n// A harmless change.\n", FIX)
    try:
        assert good.image is not None, good.failure_reason
        with capsys.disabled():
            print(f"\n[stack] sealed build {good.seconds:.0f} s; a broken file failed in {broken.seconds:.0f} s")
    finally:
        if good.image:
            remove_candidate_images([good.image])


# Hostile build output


def tar_stream(*members: tuple) -> io.BytesIO:
    data = io.BytesIO()
    with tarfile.open(fileobj=data, mode="w") as archive:
        for name, kind, value, mode in members:
            info = tarfile.TarInfo(name)
            info.mode = mode
            if kind == "file":
                info.size = len(value)
                archive.addfile(info, io.BytesIO(value))
            elif kind == "dir":
                info.type = tarfile.DIRTYPE
                archive.addfile(info)
            elif kind == "link":
                info.type = tarfile.SYMTYPE
                info.linkname = value
                archive.addfile(info)
            elif kind == "hard":
                info.type = tarfile.LNKTYPE
                info.linkname = value
                archive.addfile(info)
            elif kind == "device":
                info.type = tarfile.CHRTYPE
                archive.addfile(info)
    data.seek(0)
    return data


def test_plain_output_is_rebuilt_with_safe_modes(tmp_path: Path) -> None:
    written = extract_output(tar_stream(
        ("standalone", "dir", None, 0o777), ("standalone/server.js", "file", b"ok", 0o6755), ("standalone/data", "file", b"d", 0o666),
        ("standalone/here", "link", "server.js", 0o777),
    ), tmp_path)
    assert written == 3 and (tmp_path / "standalone/server.js").read_bytes() == b"ok"
    assert stat.S_IMODE((tmp_path / "standalone/server.js").stat().st_mode) & 0o7000 == 0
    assert stat.S_IMODE((tmp_path / "standalone/data").stat().st_mode) & 0o022 == 0
    assert (tmp_path / "standalone/here").is_symlink()


@pytest.mark.parametrize("member", [
    ("../escape", "file", b"x", 0o644), ("/etc/escape", "file", b"x", 0o644), ("a/../../escape", "file", b"x", 0o644),
    ("a/link", "link", "/etc/passwd", 0o777), ("a/link", "link", "../../etc/passwd", 0o777),
    ("a/hard", "hard", "/etc/passwd", 0o644), ("a/dev", "device", None, 0o666),
])
def test_hostile_output_is_refused(tmp_path: Path, member: tuple) -> None:
    with pytest.raises(UnsafeOutput):
        extract_output(tar_stream(member), tmp_path / "out")
    assert not (tmp_path / "escape").exists()


def test_a_file_cant_be_written_through_a_link(tmp_path: Path) -> None:
    outside = tmp_path / "outside.txt"
    outside.write_text("original")
    out = tmp_path / "out"
    out.mkdir()
    with pytest.raises((UnsafeOutput, OSError)):
        extract_output(tar_stream(("a", "link", "../outside.txt", 0o777), ("a", "file", b"changed", 0o644)), out)
    assert outside.read_text() == "original"


def test_output_over_the_byte_limit_stops_before_writing_the_file(tmp_path: Path) -> None:
    with pytest.raises(UnsafeOutput):
        extract_output(tar_stream(("big", "file", b"x" * 100, 0o644)), tmp_path, max_bytes=50)
    assert not (tmp_path / "big").exists()


def test_too_many_files_are_refused(tmp_path: Path) -> None:
    with pytest.raises(UnsafeOutput):
        extract_output(tar_stream(*[(f"f{n}", "file", b"", 0o644) for n in range(5)]), tmp_path, max_members=3)


# The gate against a cheating app


def test_the_app_gets_an_ordinary_role_and_the_trusted_side_doesnt_share_it(tmp_path: Path) -> None:
    built = session(tmp_path).argv("app-pw", 1, "admin-pw")
    assert any("POSTGRES_USER=faultforge_admin" == item for item in built["postgres"])
    assert any("POSTGRES_PASSWORD=admin-pw" == item for item in built["postgres"])
    assert not any("admin-pw" in item for item in built["app"])
    assert not any("app-pw" in item for item in built["hooks"] + built["postgres"])
    assert any("sportsdeck:app-pw@" in item for item in built["app"])
    hooks_env = next(item for item in built["hooks"] if item.startswith("FF_STACK_HOOKS="))
    assert '"database_user": "faultforge_admin"' in hooks_env
    sql = setup_role_sql(STACK, "abc")
    assert "NOSUPERUSER" in sql and "NOCREATEROLE" in sql


def test_the_host_reads_as_admin_ignoring_settings_and_every_query_is_qualified() -> None:
    argv = psql_state_argv("c", STACK, STACK.state_sql)
    assert "-X" in argv and argv[argv.index("-U") + 1] == "faultforge_admin"
    for sql in [*STACK.state_sql.values(), *STACK.reset_sql]:
        assert 'public."' in sql


def test_a_dead_trusted_service_means_the_database_is_unavailable(tmp_path: Path) -> None:
    active = session(tmp_path)
    report = model_trial.synthetic_report("run_20260101_000000_0001", "PASS")
    active.database_state = {"live_tokens": 1}
    alive = SimpleNamespace(user="70:70", running_when_inspected=True)
    dead = SimpleNamespace(user="65534:65534", running_when_inspected=False)
    active.service_facts = [alive, dead]
    assert active.state_verification(report).reason == "trusted_service_not_running"
    active.service_facts = [alive]
    assert active.state_verification(report).reason == "trusted_service_not_running"


def test_an_unchanged_file_is_refused_before_any_build(tmp_path: Path) -> None:
    def build(_code, _config):
        raise AssertionError("must not build")

    sources = {FIX.editable: GOOD_TS, **{name: "C" for name in FIX.context}}
    result = _loop(tmp_path, build, None, _reply(GOOD_TS), attempts=1, sources=sources)
    assert result.success is False and result.attempts[0].failure_reason == "code_unchanged"


def test_fallback_secrets_are_redacted_from_prompts() -> None:
    source = (
        'const JWT_SECRET = process.env.JWT_SECRET || "sportsdeck-a-value";\n'
        "const JWT_REFRESH_SECRET =\n"
        '  process.env.JWT_REFRESH_SECRET || "sportsdeck-b-value";\n'
    )
    clean = redact(source, FIX.redact)
    assert "a-value" not in clean and "b-value" not in clean and clean.count("<redacted>") == 2


def test_the_trial_uses_redaction_and_candidates_run_by_image_id(tmp_path: Path) -> None:
    assert "redact(read_image_file" in (ROOT / "scripts" / "stack_fix_trial.py").read_text(encoding="utf-8")
    seen = []

    def sandbox(spec, *_a):
        seen.append(spec.app_image)
        return SandboxResult(status="failed", failure_reason="runner_error", containment=ContainmentFacts(wall_s=1, wall_limit_s=100))

    _loop(tmp_path, lambda *_: BuildResult("tag:x", image_id="sha256:abc"), sandbox, _reply(), attempts=1)
    assert seen == ["sha256:abc"]


HOSTILE_ROUTE = """
import fs from "fs";
import net from "net";

const seen: Record<string, unknown> = { write: false, escape: false, connected: false };
seen.secrets = Object.keys(process.env).filter((name) => /SECRET|KEY|TOKEN|PASSW/i.test(name));
seen.database_url = process.env.DATABASE_URL;
seen.docker = fs.existsSync("/var/run/docker.sock");
seen.uid = process.getuid ? process.getuid() : -1;
try { fs.writeFileSync("/app/written-by-candidate", "x"); seen.write = true; } catch {}
try { fs.writeFileSync("/etc/written-by-candidate", "x"); seen.escape = true; } catch {}
await new Promise((resolve) => {
  const socket = net.connect(443, "1.1.1.1");
  socket.on("connect", () => { seen.connected = true; resolve(0); });
  socket.on("error", () => resolve(0));
  setTimeout(() => resolve(0), 2000);
});
throw new Error("FFPROBE " + JSON.stringify(seen) + " FFEND");

export async function POST() {
  return new Response("x");
}
"""


@full_build
def test_ai_code_that_runs_during_a_build_is_contained(capsys: pytest.CaptureFixture) -> None:
    """Skips the rule check on purpose, as a bypass would. The sandbox has to hold on its own."""
    result = build_candidate(HOSTILE_ROUTE, FIX)
    assert result.image is None and result.failure_reason == "build_failed"
    probe = re.search(r"FFPROBE (\{.*?\}) FFEND", result.failure_detail)
    assert probe is not None, f"the code didn't run during the build: {result.failure_detail}"
    seen = json.loads(probe[1])
    with capsys.disabled():
        print(f"\n[stack] what hostile code saw during a build: {seen}")
    # The image's own build-time placeholder, with no credentials in it.
    assert seen == {
        "write": False, "escape": False, "connected": False, "secrets": [], "docker": False, "uid": 1000,
        "database_url": "localhost:5432",
    }


@full_build
def test_a_real_model_fix_that_drops_the_signature_check_is_caught(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    """Written by glm-5.3 in a real trial. It fixes the race and passes the gate, then regression catches it."""
    code = (DEMO / "false_fixes" / "no_signature_check.ts").read_text(encoding="utf-8")
    assert check_typescript_policy(code, FIX) == ()
    built = build_candidate(code, FIX)
    assert built.image is not None, built.failure_reason
    try:
        project = tmp_path / "project"
        buggy = run_stack(StackSpec(BUGGY, STACK, CONFIG), project, RACE)
        spec = StackSpec(built.image_id, STACK, CONFIG)
        race = run_stack(spec, project, RACE, RunOptions(compare_to=buggy.report.run_id))
        assert fix_agent.gate_failures(race, buggy.deployed_code_sha256) == (), race.report.verdict_reason
        regression = fix_agent._run_regression(
            spec, project, fix_agent.regression_scenarios_for(CONFIG, RACE), RunOptions(), run_stack, race.deployed_code_sha256, CONFIG,
        )
        results = {item.scenario_id: (item.passed, item.verdict) for item in regression}
        with capsys.disabled():
            print(f"\n[stack] no-signature-check fix: race gate passed; regression {results}")
        assert results["forged-signature-rejected"] == (False, "FAIL")
        assert results["garbage-tokens-rejected"] == (True, "PASS")
    finally:
        remove_candidate_images([built.image])


@stack_images
def test_the_reference_fix_passes_every_regression_scenario(tmp_path: Path) -> None:
    spec = StackSpec(FIXED, STACK, CONFIG)
    for scenario in fix_agent.regression_scenarios_for(CONFIG, RACE):
        result = run_stack(spec, tmp_path / scenario, scenario)
        assert result.status == "completed", result.failure_detail
        assert result.report.verdict == "PASS", (scenario, result.report.verdict_reason)
        assert result.containment.state_verification.status == "matched"

