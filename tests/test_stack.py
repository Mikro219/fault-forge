"""A multi-service target: unit tests, then real containers if the app's images are built."""

import base64
import hashlib
import hmac
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from faultforge import config as config_module, fix_agent, stack_hooks
from faultforge.errors import FaultForgeError
from faultforge.lifecycle import RunOptions
from faultforge.stack import HOOKS_IMAGE, StackSession, StackSpec, STACK_POLICY, load_stack, psql_state_argv, run_stack

ROOT = Path(__file__).resolve().parents[1]
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
