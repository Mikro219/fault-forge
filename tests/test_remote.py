"""M12: the remote runtime. Everything here runs without SSH and without a VM: a fake transport
hands each request to the VM's own entry point (remote_run.serve) in this process, with fake
sandbox runners behind it, so the request, the tar going up, the tar coming back and the cleanup are
the real code's.

One test at the end is opt-in (FF_TEST_REMOTE=1) and uses the real runner VM named in the owner's
[remote] section: it needs a deploy that matches this machine (scripts/deploy_runner.sh)."""

import asyncio
import dataclasses
import functools
import io
import json
import os
import shutil
import subprocess
import tarfile
import time
from decimal import Decimal
from pathlib import Path

import pytest

from faultforge import remote_run, sandbox
from faultforge.lifecycle import RunOptions
from faultforge.mcp_server import remote as remote_module, settings as settings_module, store
from faultforge.mcp_server.remote import Completed, DeployRecord, RemoteConfig
from faultforge.mcp_server.tools import REMOTE_IMAGE_ONLY, Tools
from faultforge.node_build import StackBuild
from faultforge.sandbox import SandboxResult, TargetSpec

from test_mcp import (  # noqa: F401 - project and no_real_key_files are fixtures
    BILLING_KEY, INFERENCE_KEY, SCENARIO, TICKET_API, FixSandbox, Recorder, deployed_sha, fix_the_code, guard_with,
    key_file, no_real_key_files, project, rows,
)
from faultforge.mcp_server import credit

HOST = "203.0.113.7"
CODE = "c0" * 32
IMAGES = {name: f"sha256:{index:064x}" for index, name in enumerate(remote_module.required_images(), start=1)}
NAME = "1f" * 20


def remote_config(tmp_path: Path, roots: list[str], extra: str = "") -> tuple[Path, Path]:
    keys = tmp_path / "ssh"
    keys.mkdir(exist_ok=True)
    (keys / "runner_key").write_text("not a real key\n", encoding="utf-8")
    (keys / "known_hosts").write_text(f"{HOST} ssh-ed25519 AAAAnotreal\n", encoding="utf-8")
    config = tmp_path / "mcp.toml"
    config.write_text(
        f"allowed_roots = {json.dumps(roots)}\n\n[remote]\nhost = \"{HOST}\"\n"
        f"key_path = \"{keys / 'runner_key'}\"\nknown_hosts_path = \"{keys / 'known_hosts'}\"\n{extra}",
        encoding="utf-8",
    )
    return config, tmp_path / "state"


def setup_remote(monkeypatch, tmp_path: Path, project: Path, *, record: bool = True, extra: str = "", images: dict | None = None) -> Path:
    config, state = remote_config(tmp_path, [str(project.parent)], extra)
    monkeypatch.setenv(settings_module.CONFIG_ENV, str(config))
    monkeypatch.setenv(settings_module.STATE_ENV, str(state))
    if record:
        remote_module.write_record(state, DeployRecord(
            host=HOST, user="runner", name=NAME, commit=NAME, code_sha256=CODE, images=dict(images or IMAGES), uv="0.12.18",
            deployed_at="2026-11-16T00:00:00+00:00",
        ))
    return state


class Honest:
    """Wraps a fake sandbox so it reports the hash a real one would: of the files it was sent."""

    def __init__(self, inner):
        self.inner = inner

    def __call__(self, spec: TargetSpec, work_dir: Path, scenario_id: str, options) -> SandboxResult:
        result = self.inner(spec, work_dir, scenario_id, options)
        return result.model_copy(update={"deployed_code_sha256": deployed_sha(spec)})


class FakeVM:
    """The transport: each call goes to remote_run.serve, as the VM's sshd would hand it over."""

    def __init__(self, base: Path, *, run_sandboxed=None, build_stack=None, run_stack=None, code: str = CODE, images: dict | None = None):
        self.base = base
        self.calls: list[tuple[list[str], dict, bytes]] = []
        self.removed: list[list[str]] = []
        self.down = False
        self.runners = remote_run.Runners(
            run_sandboxed=run_sandboxed, run_stack=run_stack, build_stack=build_stack,
            remove_images=self.removed.append, image_ids=lambda names: {name: (images or IMAGES).get(name) for name in names},
            sandbox_count=lambda: 0, reap_containers=lambda: None, candidate_images=lambda ids: [item for item in ids if item.endswith("1")],
            code_sha256=lambda: code,
        )

    def call(self, argv: list[str], stdin: bytes, timeout_s: float, max_stdout: int) -> Completed:
        request = json.loads(stdin.split(b"\n", 1)[0])
        self.calls.append((argv, request, stdin))
        if self.down:
            return Completed(255, b"", b"ssh: connect to host: Connection timed out\n")
        out = io.BytesIO()
        code = remote_run.serve(io.BytesIO(stdin), out, base=self.base, runners=self.runners)
        return Completed(code, out.getvalue(), b"")

    def ops(self) -> list[str]:
        return [request["op"] for _, request, _ in self.calls]


def tools_for(tmp_path: Path, vm: FakeVM, *, balance: float = -200.0, local=None, key: bool = True, **kwargs) -> Tools:
    guard, _, _, _ = guard_with(tmp_path, (balance, 0.0), key=key)
    check = functools.partial(remote_module.static_check, image_ids=lambda names: {name: IMAGES.get(name) for name in names}, code_sha256=lambda: CODE)
    return Tools(run_sandboxed=local or Recorder(), credit=guard, remote_transport=vm, remote_check=check, **kwargs)


def vm_snapshots(base: Path) -> list[str]:
    folder = base / remote_run.SNAPSHOTS
    return sorted(entry.name for entry in folder.iterdir()) if folder.is_dir() else []


# SSH: the argv, the environment and the deadlines


def test_the_ssh_argv_pins_the_host_key_and_never_interpolates() -> None:
    config = RemoteConfig(host=HOST, key_path=Path("/home/u/.ssh/k"), known_hosts_path=Path("/home/u/.config/faultforge/known"))
    argv = remote_module.ssh_argv(config, remote_module.remote_command(NAME))
    options = [argv[index + 1] for index, item in enumerate(argv) if item == "-o"]
    for expected in (
        "BatchMode=yes", "StrictHostKeyChecking=yes", "UserKnownHostsFile=/home/u/.config/faultforge/known", "GlobalKnownHostsFile=/dev/null",
        "IdentitiesOnly=yes", "IdentityAgent=none", "ForwardAgent=no", "ClearAllForwardings=yes", "RequestTTY=no", "ConnectTimeout=10",
        "ServerAliveInterval=15", "PasswordAuthentication=no", "UpdateHostKeys=no",
    ):
        assert expected in options
    assert argv[:3] == ["ssh", "-F", "/dev/null"]
    assert argv[argv.index("-i") + 1] == "/home/u/.ssh/k" and argv[argv.index("-l") + 1] == "runner"
    tail = argv[argv.index("--"):]
    assert tail == ["--", HOST, f"/home/runner/faultforge/app/{NAME}/.venv/bin/python", "-I", "-m", "faultforge.remote_run"]
    # ssh joins the command for the VM's shell, so nothing but plain tokens may reach it.
    for name in ("HEAD", "1f" * 19, "../../etc", "1f" * 20 + ";rm", ""):
        with pytest.raises(ValueError):
            remote_module.remote_command(name)
    for token in ("a b", "x;y", "$(id)", "`id`", "a'b", "a|b", "a\nb"):
        with pytest.raises(ValueError):
            remote_module.ssh_argv(config, ["python", token])


def test_ssh_gets_no_agent_and_no_key_in_its_environment(monkeypatch) -> None:
    for name, value in {"SSH_AUTH_SOCK": "/tmp/agent", "VULTR_API_KEY": "secret", "VULTR_INFERENCE_API_KEY": "secret", "AWS_SECRET_ACCESS_KEY": "s"}.items():
        monkeypatch.setenv(name, value)
    env = remote_module.ssh_env()
    assert set(env) <= {"PATH", "HOME", "LC_ALL"} and "secret" not in json.dumps(env)


def test_the_real_transport_kills_on_a_timeout_and_on_an_overflow() -> None:
    transport = remote_module.SshTransport()
    started = time.monotonic()
    slow = transport.call(["sh", "-c", "sleep 30"], b"", 0.5, 1000)
    assert slow.timed_out and time.monotonic() - started < 10
    loud = transport.call(["sh", "-c", "yes"], b"", 20, 10_000)
    assert loud.overflowed and len(loud.stdout) <= 10_000
    echo = transport.call(["cat"], b"hello\n", 10, 1000)
    assert echo.stdout == b"hello\n" and echo.returncode == 0 and not echo.timed_out


# The [remote] section


def test_the_remote_section_and_its_defaults(monkeypatch, tmp_path) -> None:
    parsed = remote_module.parse_remote({"host": HOST})
    assert (parsed.user, parsed.hourly_usd, parsed.auto) == ("runner", 0.066, "remote")
    assert parsed.key_path == Path("~/.ssh/faultforge_vultr").expanduser()
    for bad in (
        {}, {"host": "Example.com"}, {"host": HOST, "user": "root"}, {"host": HOST, "hourly_usd": 0}, {"host": HOST, "hourly_usd": "1"},
        {"host": HOST, "hourly_usd": float("inf")}, {"host": HOST, "key_path": "relative/key"}, {"host": HOST, "key_path": "/a b/key"},
        {"host": HOST, "auto": "always"}, {"host": HOST, "port": 22}, {"host": "1.2.3.4; id"},
    ):
        with pytest.raises(ValueError):
            remote_module.parse_remote(bad)
    # A bad [remote] disables the remote runtime only: the roots still apply.
    config = tmp_path / "mcp.toml"
    config.write_text(f'allowed_roots = ["{tmp_path}"]\n[remote]\nhost = "{HOST}"\nuser = "root"\n', encoding="utf-8")
    monkeypatch.setenv(settings_module.CONFIG_ENV, str(config))
    loaded = settings_module.load_settings()
    assert loaded.allowed_roots == (tmp_path.resolve(),) and loaded.remote is None and "user" in loaded.remote_problem
    config.write_text(f'allowed_roots = ["{tmp_path}"]\n', encoding="utf-8")
    assert settings_module.load_settings().remote_problem == "no [remote] section in mcp.toml"


# A run in remote mode, through the VM's real entry point


def test_a_remote_run_sends_the_snapshot_runs_there_and_brings_the_run_back(monkeypatch, tmp_path, project) -> None:
    state = setup_remote(monkeypatch, tmp_path, project)
    (project / ".env").write_text("SECRET=hunter2\n", encoding="utf-8")
    key_file(tmp_path, credit.INFERENCE_KEY_NAME, INFERENCE_KEY)
    vm_side = Recorder()
    vm = FakeVM(tmp_path / "vm", run_sandboxed=Honest(vm_side))
    local = Recorder()
    result = tools_for(tmp_path, vm, local=local).run_scenario(str(project), SCENARIO, "worktree", runtime="remote")
    assert result["status"] == "ok" and result["verdict"] == "FAIL", result
    assert result["runtime"] == "remote" and result["source_left_this_machine"] is True
    assert result["runtime_reason"].startswith("requested: credit $200.00 stays at or above the $10.00 floor")
    assert "worst case is $0.066 at $0.066/h" in result["runtime_reason"]
    assert HOST in result["remote"] and "No key" in result["remote"]
    assert local.calls == [] and vm.ops() == ["ping", "run_python"]
    # What went up: the app, the package, the pinned config and the run directory, in one tar; no secret file and no key.
    argv, request, stdin = vm.calls[1]
    assert set(request) == {"op", "scenario_id", "options", "support", "state_verifier", "expect"}
    assert request["expect"] == {"code_sha256": CODE, "images": IMAGES} and request["support"] == "ticket_support"
    with tarfile.open(fileobj=io.BytesIO(stdin.split(b"\n", 1)[1])) as archive:
        names = archive.getnames()
    assert "app.py" in names and "config/faultforge.yaml" in names and "support/ticket_support/__init__.py" in names
    assert not any(".env" in name for name in names)
    for _, _, sent in vm.calls:
        assert b"hunter2" not in sent and BILLING_KEY.encode() not in sent and INFERENCE_KEY.encode() not in sent
    # The VM ran the same spec a local run would have.
    spec = vm_side.calls[0][0]
    assert spec.app_source == (project / "app.py").read_bytes() and spec.allow_hostile is False and spec.state_verifier
    # The run came back and is stored here, marked remote, and nothing is left on either side.
    meta = store.read_meta(state, result["run_id"])
    assert meta.runtime == "remote" and any("left this machine" in note for note in meta.notes)
    assert store.evidence(state, result["run_id"], 0, 5, None, None)["total"] == 7
    assert vm_snapshots(vm.base) == [] and list((state / "snapshots").iterdir()) == [] and list((state / "work").iterdir()) == []


def test_a_local_run_says_nothing_left(monkeypatch, tmp_path, project) -> None:
    setup_remote(monkeypatch, tmp_path, project)
    vm = FakeVM(tmp_path / "vm")
    result = tools_for(tmp_path, vm).run_scenario(str(project), SCENARIO, "worktree", runtime="local")
    assert result["runtime"] == "local" and result["source_left_this_machine"] is False and vm.calls == []


@pytest.mark.parametrize("balance,allowed", [
    (-10.066, True),      # exactly $10.00 after one hour at $0.066
    (-200.0, True),
    (-10.065, False),
    (-5.0, False),
])
def test_the_credit_floor_gates_a_remote_run_either_side(monkeypatch, tmp_path, project, balance, allowed) -> None:
    setup_remote(monkeypatch, tmp_path, project)
    vm = FakeVM(tmp_path / "vm", run_sandboxed=Honest(Recorder()))
    tools = tools_for(tmp_path, vm, balance=balance)
    explicit = tools.run_scenario(str(project), SCENARIO, "worktree", runtime="remote")
    if allowed:
        assert explicit["status"] == "ok" and explicit["runtime"] == "remote"
    else:
        assert explicit["error_class"] == "credit_floor" and "below the $10.00 floor after the worst case" in explicit["message"]
        # Refused before anything was sent, the ping included.
        assert vm.calls == []
        auto = tools.run_scenario(str(project), SCENARIO, "worktree")
        assert auto["status"] == "ok" and auto["runtime"] == "local" and "below the $10.00 floor" in auto["runtime_reason"]
        assert vm.calls == []


def test_without_a_billing_key_remote_fails_closed(monkeypatch, tmp_path, project) -> None:
    setup_remote(monkeypatch, tmp_path, project)
    vm = FakeVM(tmp_path / "vm")
    tools = tools_for(tmp_path, vm, key=False)
    assert tools.run_scenario(str(project), SCENARIO, "worktree", runtime="remote")["error_class"] == "credit_floor"
    auto = tools.run_scenario(str(project), SCENARIO, "worktree")
    assert auto["runtime"] == "local" and auto["runtime_reason"] == "auto resolves to local: remote: no billing key" and vm.calls == []


def test_the_worst_case_counts_every_step_a_call_can_make() -> None:
    config = RemoteConfig(host=HOST, hourly_usd=0.066)
    one, wall = remote_module.worst_case_usd(config, {"run_python": 1})
    assert one == Decimal("0.066") and wall == remote_module.wall_s("ping") + remote_module.wall_s("run_python")
    # A verify with 20 regression scenarios can take hours of wall limit: each hour is charged.
    many, wall = remote_module.worst_case_usd(config, {"run_python": 21})
    assert wall > 3 * 3600 and many == Decimal("0.066") * ((int(wall) // 3600) + 1)


@pytest.mark.parametrize("case,reason", [
    ("no section", "no [remote] section in mcp.toml"),
    ("no record", "FaultForge isn't deployed to the runner VM"),
    ("other code", "isn't the code on this machine"),
    ("other images", "the deployed images aren't this machine's"),
    ("lacks an image", "the deploy lacks faultforge-egress-proxy:dev"),
    ("vm down", "the runner VM didn't answer (the VM couldn't be reached)"),
    ("vm runs other code", "the runner VM runs other FaultForge code"),
    ("auto prefers local", '[remote] auto is "local"'),
])
def test_auto_falls_back_to_local_and_says_why(monkeypatch, tmp_path, project, case, reason) -> None:
    images = dict(IMAGES)
    if case == "lacks an image":
        images.pop("faultforge-egress-proxy:dev")
    state = setup_remote(
        monkeypatch, tmp_path, project, record=case != "no record", images=images, extra='auto = "local"\n' if case == "auto prefers local" else "",
    )
    if case == "no section":
        (tmp_path / "mcp.toml").write_text(f"allowed_roots = {json.dumps([str(project.parent)])}\n", encoding="utf-8")
    vm = FakeVM(tmp_path / "vm", code="ee" * 32 if case == "vm runs other code" else CODE)
    vm.down = case == "vm down"
    guard, _, _, _ = guard_with(tmp_path, (-200.0, 0.0))
    local_images = {**IMAGES, "faultforge-runner:dev": "sha256:" + "9" * 64} if case == "other images" else IMAGES
    check = functools.partial(
        remote_module.static_check, image_ids=lambda names: {name: local_images.get(name) for name in names},
        code_sha256=lambda: "dd" * 32 if case == "other code" else CODE,
    )
    local = Recorder()
    tools = Tools(run_sandboxed=local, credit=guard, remote_transport=vm, remote_check=check)
    auto = tools.run_scenario(str(project), SCENARIO, "worktree")
    assert auto["status"] == "ok" and auto["runtime"] == "local" and len(local.calls) == 1, auto
    assert auto["runtime_reason"].startswith("auto resolves to local: ") and reason in auto["runtime_reason"], auto["runtime_reason"]
    assert vm.ops() in ([], ["ping"]) and auto["source_left_this_machine"] is False
    status = tools.status()
    assert status["runtimes"]["remote"]["available"] is (case == "auto prefers local")
    assert status["effective_modes"]["runtime"].startswith("auto -> local: ")
    if case != "auto prefers local":
        explicit = tools.run_scenario(str(project), SCENARIO, "worktree", runtime="remote")
        assert explicit["error_class"] == "runtime_unavailable" and reason in explicit["message"]
        if case in ("other code", "other images", "no record", "lacks an image", "vm runs other code"):
            assert "scripts/deploy_runner.sh" in explicit["fix_hint"]
    assert len(local.calls) == 1 and list((state / "snapshots").iterdir()) == []


def test_status_shows_the_remote_runtime(monkeypatch, tmp_path, project) -> None:
    setup_remote(monkeypatch, tmp_path, project)
    vm = FakeVM(tmp_path / "vm")
    status = tools_for(tmp_path, vm).status()
    remote = status["runtimes"]["remote"]
    assert remote["available"] is True and remote["host"] == HOST and remote["hourly_usd"] == 0.066 and remote["deployed"].startswith(NAME[:12])
    assert "credit $200.00 stays at or above" in remote["reason"] and "never a key" in remote["what_leaves"]
    assert status["effective_modes"]["runtime"].startswith("auto -> remote: credit $200.00")
    assert vm.ops() == ["ping"]


def test_a_prebuilt_stack_image_never_runs_remotely(monkeypatch, tmp_path) -> None:
    from test_mcp import SPORTSDECK_IMAGE, git

    repo = tmp_path / "repo"
    shutil.copytree(Path(__file__).resolve().parents[1] / "demo" / "sportsdeck", repo, ignore=shutil.ignore_patterns("replays", "false_fixes", "reference_fix"))
    git(repo, "init", "-q")
    config, state = remote_config(tmp_path, [str(repo)])
    config.write_text(config.read_text(encoding="utf-8").replace("\n\n[remote]", f'\nallowed_images = ["{SPORTSDECK_IMAGE}"]\n\n[remote]'), encoding="utf-8")
    monkeypatch.setenv(settings_module.CONFIG_ENV, str(config))
    monkeypatch.setenv(settings_module.STATE_ENV, str(state))
    vm = FakeVM(tmp_path / "vm")
    tools = tools_for(tmp_path, vm)
    scenario = next(iter(__import__("yaml").safe_load((repo / "faultforge.yaml").read_text())["scenarios"]))["id"]
    explicit = tools.run_scenario(str(repo), scenario, "image", runtime="remote")
    assert explicit["error_class"] == "runtime_unavailable" and explicit["message"] == REMOTE_IMAGE_ONLY and vm.calls == []


# The tar going up and coming back


def _tar(*members: tuple[str, str, bytes | str]) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        for kind, name, value in members:
            info = tarfile.TarInfo(name)
            if kind == "file":
                info.size = len(value)
                archive.addfile(info, io.BytesIO(value))
            elif kind == "dir":
                info.type = tarfile.DIRTYPE
                archive.addfile(info)
            elif kind == "sym":
                info.type, info.linkname = tarfile.SYMTYPE, value
                archive.addfile(info)
            elif kind == "hard":
                info.type, info.linkname = tarfile.LNKTYPE, value
                archive.addfile(info)
            elif kind == "fifo":
                info.type = tarfile.FIFOTYPE
                archive.addfile(info)
    return buffer.getvalue()


@pytest.mark.parametrize("members", [
    [("sym", "work/link", "/etc/passwd")],
    [("sym", "work/link", "inside")],
    [("hard", "work/hard", "work/a")],
    [("fifo", "work/pipe", b"")],
    [("file", "../escape", b"x")],
    [("file", "/abs", b"x")],
    [("file", "work/../../escape", b"x")],
    [("file", "other/x", b"x")],
    [("file", "work/a", b"x"), ("file", "work/a", b"y")],
    [("file", "work/big", b"x" * 2048)],
])
def test_unpack_takes_only_plain_files_in_the_asked_directories(tmp_path, members) -> None:
    destination = tmp_path / "out"
    destination.mkdir()
    with pytest.raises((remote_run.Refused, FileExistsError)):
        remote_run.unpack(io.BytesIO(_tar(*members)), destination, 1024, ("work",))
    assert not (tmp_path / "escape").exists()


def test_pack_refuses_links_and_round_trips_plain_trees(tmp_path) -> None:
    tree = tmp_path / "tree"
    (tree / "a" / "b").mkdir(parents=True)
    (tree / "a" / "b" / "f.txt").write_text("hi", encoding="utf-8")
    data = remote_run.pack({"work": tree, "app.py": b"print(1)\n"})
    out = tmp_path / "out"
    out.mkdir()
    remote_run.unpack(io.BytesIO(data), out, 1 << 20, ("work", "app.py"))
    assert (out / "work" / "a" / "b" / "f.txt").read_text() == "hi" and (out / "app.py").read_bytes() == b"print(1)\n"
    (tree / "link").symlink_to("/etc/passwd")
    with pytest.raises(remote_run.Refused):
        remote_run.pack({"work": tree})


class TamperingTransport(FakeVM):
    """A VM whose answer is replaced after the fact, to check what the host accepts."""

    def __init__(self, base: Path, tamper, **kwargs):
        super().__init__(base, **kwargs)
        self.tamper = tamper

    def call(self, argv, stdin, timeout_s, max_stdout) -> Completed:
        completed = super().call(argv, stdin, timeout_s, max_stdout)
        if json.loads(stdin.split(b"\n", 1)[0])["op"] == "ping":
            return completed
        return dataclasses.replace(completed, **self.tamper(completed))


def _answer_with(archive: bytes, **changes) -> callable:
    def tamper(completed: Completed) -> dict:
        head, _, _ = completed.stdout.partition(b"\n")
        answer = {**json.loads(head), **changes, "tar_bytes": len(archive)}
        return {"stdout": json.dumps(answer).encode() + b"\n" + archive}
    return tamper


@pytest.mark.parametrize("tamper,message", [
    (_answer_with(_tar(("sym", "work/.faultforge", "/etc"))), "the run directory that came back was refused"),
    (_answer_with(_tar(("file", "../../escape", b"x"))), "the run directory that came back was refused"),
    (_answer_with(b"not a tar at all"), "the run directory that came back was refused"),
    (lambda completed: {"stdout": completed.stdout[:-10]}, "the answer's archive is incomplete"),
    (lambda completed: {"stdout": b"{not json\n"}, "the answer isn't JSON"),
    (lambda completed: {"stdout": b""}, "the VM sent no answer"),
    (lambda completed: {"timed_out": True}, "no answer within"),
    (lambda completed: {"overflowed": True}, "more than the cap"),
    (_answer_with(b"", ok=False, error="nope"), "the VM refused: nope"),
    (_answer_with(b"", snapshot_removed=False), "couldn't remove"),
    (_answer_with(b"", result={"status": "completed"}), "isn't a valid sandbox result"),
])
def test_a_bad_answer_is_refused_and_nothing_is_stored(monkeypatch, tmp_path, project, tamper, message) -> None:
    state = setup_remote(monkeypatch, tmp_path, project)
    vm = TamperingTransport(tmp_path / "vm", tamper, run_sandboxed=Honest(Recorder()))
    result = tools_for(tmp_path, vm).run_scenario(str(project), SCENARIO, "worktree", runtime="remote")
    assert result["status"] == "error" and result["error_class"] == "sandbox_failed" and message in result["message"], result
    assert not (tmp_path / "escape").exists()
    assert not (state / "runs").exists() or list((state / "runs").iterdir()) == []
    assert list((state / "work").iterdir()) == [] and list((state / "snapshots").iterdir()) == []


def test_the_vm_must_report_the_code_it_was_sent(monkeypatch, tmp_path, project) -> None:
    state = setup_remote(monkeypatch, tmp_path, project)
    vm = FakeVM(tmp_path / "vm", run_sandboxed=Recorder())   # reports a hash of nothing it was sent
    result = tools_for(tmp_path, vm).run_scenario(str(project), SCENARIO, "worktree", runtime="remote")
    assert result["error_class"] == "sandbox_failed" and "other code than was sent" in result["message"]
    assert not (state / "runs").exists() or list((state / "runs").iterdir()) == []


# The VM side: the deploy check, the cleanup and the reaper


def _request(op: str = "run_python", **fields) -> dict:
    base = {"op": op, "scenario_id": SCENARIO, "options": dataclasses.asdict(RunOptions()), "support": None, "state_verifier": None,
            "expect": {"code_sha256": CODE, "images": IMAGES}}
    return {**base, **fields}


def _serve(base: Path, request: dict, inputs: bytes, runners: remote_run.Runners) -> tuple[dict, bytes]:
    out = io.BytesIO()
    remote_run.serve(io.BytesIO(json.dumps(request).encode() + b"\n" + inputs), out, base=base, runners=runners)
    head, _, rest = out.getvalue().partition(b"\n")
    return json.loads(head), rest


def _inputs(tmp_path: Path) -> bytes:
    config = tmp_path / "faultforge.yaml"
    shutil.copyfile(TICKET_API / "faultforge.yaml", config)
    return remote_run.pack({"app.py": (TICKET_API / "app.py").read_bytes(), "config/faultforge.yaml": config})


def test_the_vm_refuses_a_deploy_it_doesnt_match_before_anything(tmp_path) -> None:
    ran = []
    vm = FakeVM(tmp_path / "vm", run_sandboxed=lambda *args: ran.append(args))
    for expect in ({"code_sha256": "ee" * 32, "images": IMAGES}, {"code_sha256": CODE, "images": {**IMAGES, "faultforge-target:dev": "sha256:" + "0" * 64}}, None):
        answer, archive = _serve(vm.base, _request(expect=expect), _inputs(tmp_path), vm.runners)
        assert answer["ok"] is False and archive == b"" and ran == []
        assert expect is None or answer["error"].startswith("deploy_mismatch")
    assert vm_snapshots(vm.base) == []


@pytest.mark.parametrize("behaviour", ["completes", "raises", "deadline", "bad input"])
def test_the_vm_removes_the_snapshot_on_every_path(tmp_path, behaviour) -> None:
    seen: list[Path] = []

    def run(spec, work, scenario_id, options):
        seen.append(work.parent)
        assert work.parent.parent == tmp_path / "vm" / remote_run.SNAPSHOTS and (work.parent / "app.py").is_file()
        if behaviour == "raises":
            raise RuntimeError("boom with project text")
        if behaviour == "deadline":
            raise remote_run.Deadline()
        return Honest(Recorder())(spec, work, scenario_id, options)

    vm = FakeVM(tmp_path / "vm", run_sandboxed=run)
    inputs = _tar(("sym", "app.py", "/etc/passwd")) if behaviour == "bad input" else _inputs(tmp_path)
    answer, archive = _serve(vm.base, _request(), inputs, vm.runners)
    assert answer["ok"] is (behaviour == "completes") and answer.get("snapshot_removed") is True
    assert vm_snapshots(vm.base) == [] and all(not path.exists() for path in seen)
    if behaviour == "raises":
        assert answer["error"] == "unexpected RuntimeError on the VM" and "project text" not in json.dumps(answer)
    if behaviour == "completes":
        assert answer["tar_bytes"] == len(archive) > 0


def test_the_reaper_removes_only_old_request_directories(tmp_path) -> None:
    base = tmp_path / "vm"
    folder = remote_run.snapshots_dir(base)
    old, young, foreign = folder / ("a" * 16), folder / ("b" * 16), folder / "keep-me"
    for path in (old, young, foreign):
        path.mkdir()
    stale = time.time() - 3 * 3600
    os.utime(old, (stale, stale))
    os.utime(foreign, (stale, stale))
    assert remote_run.reap(base) == 1
    assert sorted(entry.name for entry in folder.iterdir()) == sorted([young.name, foreign.name])


def test_a_ping_reaps_old_request_directories_and_every_acting_request_reaps_containers_too(tmp_path) -> None:
    """An SSH session dropped mid-run leaves its request directory behind (the process is killed, so
    no finally runs). The next ping or request reaps it once it is two hours old."""
    reaped = []
    vm = FakeVM(tmp_path / "vm", run_sandboxed=Honest(Recorder()))
    vm.runners.reap_containers = lambda: reaped.append(1)
    stale = time.time() - 3 * 3600

    def orphan(char: str) -> Path:
        path = remote_run.snapshots_dir(vm.base) / (char * 16)
        (path / "work").mkdir(parents=True)
        (path / "app.py").write_text("left by a killed request\n")
        os.utime(path, (stale, stale))
        return path

    orphan("c")
    young = remote_run.snapshots_dir(vm.base) / ("d" * 16)
    young.mkdir()
    answer, _ = _serve(vm.base, {"op": "ping", "images": list(IMAGES)}, b"", vm.runners)
    assert answer["ok"] and answer["reaped_snapshots"] == 1 and answer["snapshots"] == 1 and answer["code_sha256"] == CODE
    assert reaped == [] and vm_snapshots(vm.base) == ["d" * 16]
    shutil.rmtree(young)
    orphan("e")
    answer, _ = _serve(vm.base, _request(), _inputs(tmp_path), vm.runners)
    assert answer["ok"] and answer["reaped_snapshots"] == 1 and reaped == [1] and vm_snapshots(vm.base) == []


def test_images_are_removed_only_by_id_and_only_if_built_there(tmp_path) -> None:
    vm = FakeVM(tmp_path / "vm")
    built, other = "sha256:" + "a" * 63 + "1", "sha256:" + "b" * 64
    answer, _ = _serve(vm.base, {"op": "remove_images", "images": [built, other], "expect": {"code_sha256": CODE, "images": IMAGES}}, b"", vm.runners)
    assert answer["ok"] and answer["removed"] == 1 and vm.removed == [[built]]
    for images in (["faultforge-target:dev"], ["sha256:abc"], "sha256:" + "a" * 64):
        answer, _ = _serve(vm.base, {"op": "remove_images", "images": images, "expect": {"code_sha256": CODE, "images": IMAGES}}, b"", vm.runners)
        assert answer["ok"] is False
    assert vm.removed == [[built]]


def test_a_request_that_names_a_path_or_a_bad_option_is_refused(tmp_path) -> None:
    vm = FakeVM(tmp_path / "vm", run_sandboxed=Honest(Recorder()))
    for request in (
        _request(support="../../etc"), _request(scenario_id="../x"), _request(options={"compare_to": "../../x"}),
        _request(options={"vus": "10"}), _request(options={"shell": "id"}), _request(op="exec"), _request(state_verifier=["x"]),
    ):
        answer, _ = _serve(vm.base, request, _inputs(tmp_path), vm.runners)
        assert answer["ok"] is False, request
    assert vm_snapshots(vm.base) == []


# verify_fix and a stack build in remote mode


def remote_baseline(tmp_path: Path, project: Path) -> tuple[FakeVM, dict]:
    vm = FakeVM(tmp_path / "vm", run_sandboxed=Honest(Recorder()))
    baseline = tools_for(tmp_path, vm).run_scenario(str(project), SCENARIO, "worktree", runtime="remote")
    assert baseline["runtime"] == "remote" and baseline["gate_trust"] == "remote-reported", baseline
    vm.runners.run_sandboxed = Honest(FixSandbox())
    vm.calls.clear()
    return vm, baseline


def test_verify_fix_follows_its_baseline_onto_the_vm_and_says_whose_rows_it_trusts(monkeypatch, tmp_path, project) -> None:
    state = setup_remote(monkeypatch, tmp_path, project)
    vm, baseline = remote_baseline(tmp_path, project)
    fix_the_code(project)
    result = tools_for(tmp_path, vm).verify_fix(baseline["run_id"])
    assert result["verified"] is True and all(rows(result).values()), result
    assert result["gate_trust"] == "remote-reported" and result["source_left_this_machine"] is True
    assert f"runtime remote (the baseline {baseline['run_id']} ran remote: credit" in result["source"]
    assert vm.ops() == ["ping", "run_python", "run_python", "run_python"]
    assert store.summary(state, result["run_id"])["gate_trust"] == "remote-reported"
    assert store.read_meta(state, result["run_id"]).runtime == "remote" and vm_snapshots(vm.base) == []
    # The worst case counted all three runs.
    assert "wall limit is" in result["source"]


@pytest.mark.parametrize("baseline_runtime,asked", [("local", "remote"), ("remote", "local")])
def test_a_verify_on_another_runtime_than_its_baseline_is_not_comparable(monkeypatch, tmp_path, project, baseline_runtime, asked) -> None:
    setup_remote(monkeypatch, tmp_path, project)
    if baseline_runtime == "remote":
        vm, baseline = remote_baseline(tmp_path, project)
    else:
        vm = FakeVM(tmp_path / "vm", run_sandboxed=Honest(FixSandbox()))
        baseline = tools_for(tmp_path, vm).run_scenario(str(project), SCENARIO, "worktree", runtime="local")
        assert baseline["gate_trust"] == "local" and baseline["source_left_this_machine"] is False
    fix_the_code(project)
    local = Recorder()
    result = tools_for(tmp_path, vm, local=local).verify_fix(baseline["run_id"], runtime=asked)
    assert result["error_class"] == "not_comparable" and f"ran on the {baseline_runtime} runtime" in result["message"], result
    assert vm.calls == [] and local.calls == []


def test_a_local_baseline_verifies_locally_under_auto(monkeypatch, tmp_path, project) -> None:
    setup_remote(monkeypatch, tmp_path, project)
    baseline = Tools(run_sandboxed=Recorder()).run_scenario(str(project), SCENARIO, "worktree", runtime="local")
    fix_the_code(project)
    vm = FakeVM(tmp_path / "vm")
    result = tools_for(tmp_path, vm, local=FixSandbox()).verify_fix(baseline["run_id"])
    assert result["verified"] is True and result["gate_trust"] == "local" and result["source_left_this_machine"] is False
    assert vm.calls == []


def test_a_remote_failure_during_verify_is_an_error_and_stores_nothing(monkeypatch, tmp_path, project) -> None:
    state = setup_remote(monkeypatch, tmp_path, project)
    vm0, baseline = remote_baseline(tmp_path, project)
    fix_the_code(project)
    calls = []
    honest = Honest(FixSandbox())

    def flaky(spec, work, scenario_id, options):
        calls.append(scenario_id)
        if len(calls) == 2:
            raise remote_run.Deadline()
        return honest(spec, work, scenario_id, options)

    vm = FakeVM(tmp_path / "vm", run_sandboxed=flaky)
    runs_before = sorted(entry.name for entry in (state / "runs").iterdir())
    result = tools_for(tmp_path, vm).verify_fix(baseline["run_id"])
    assert result["status"] == "error" and result["error_class"] == "sandbox_failed" and "deadline" in result["message"], result
    # The snapshot had gone up before the failure, and the error says so.
    assert result["source_left_this_machine"] is True and result["gate_trust"] == "remote-reported"
    assert sorted(entry.name for entry in (state / "runs").iterdir()) == runs_before
    assert vm_snapshots(vm.base) == [] and list((state / "work").iterdir()) == []


def test_a_remote_build_must_be_of_the_source_that_was_sent(tmp_path) -> None:
    from faultforge.node_build import BuildConfig

    source = tmp_path / "source"
    (source / "src").mkdir(parents=True)
    (source / "src" / "x.ts").write_text("export const x = 1\n", encoding="utf-8")
    build = BuildConfig(kind="node-pnpm", node="24.22.3", pnpm="12.21.0", output=["build"], start=["node", "build"])
    image = "sha256:" + "e" * 64

    def honest_build(src, config, state, *, allow_fetch):
        from faultforge import node_build
        assert state == tmp_path / "vm" / "state" and allow_fetch is False and config == build
        return StackBuild(image_id=image, source_sha256=node_build.source_sha256(src), install="cached", toolchain={"build": "b", "run": "r"})

    record = DeployRecord(host=HOST, user="runner", name=NAME, code_sha256=CODE, images=IMAGES, deployed_at="x")
    config = RemoteConfig(host=HOST)
    vm = FakeVM(tmp_path / "vm", build_stack=honest_build)
    runtime = remote_module.RemoteRuntime(config, record, CODE, vm)
    built = runtime.build_stack(source, build, tmp_path / "local-state", allow_fetch=False)
    assert built.image_id == image and built.install == "cached" and vm_snapshots(vm.base) == []
    lying = FakeVM(tmp_path / "vm", build_stack=lambda *args, **kwargs: StackBuild(image_id=image, source_sha256="0" * 64))
    with pytest.raises(remote_module.RemoteFailure, match="other source"):
        remote_module.RemoteRuntime(config, record, CODE, lying).build_stack(source, build, tmp_path, allow_fetch=False)


def test_a_vultr_fix_runs_its_candidates_locally_even_with_a_usable_remote(monkeypatch, tmp_path, project) -> None:
    from test_mcp import REFERENCE_FIX, Account, Model, vultr_tools

    setup_remote(monkeypatch, tmp_path, project)
    run = Tools(run_sandboxed=Recorder()).run_scenario(str(project), SCENARIO, "worktree", runtime="local")
    vm = FakeVM(tmp_path / "vm", run_sandboxed=Honest(FixSandbox()))
    check = functools.partial(remote_module.static_check, image_ids=lambda names: {name: IMAGES.get(name) for name in names}, code_sha256=lambda: CODE)
    local = FixSandbox()
    tools = vultr_tools(tmp_path, Account((-200.0, 0.0)), Model(REFERENCE_FIX), local, remote_transport=vm, remote_check=check)
    assert tools.status()["runtimes"]["remote"]["available"] is True   # remote would be allowed
    vm.calls.clear()
    result = tools.propose_fix(run["run_id"], "vultr")
    assert result["status"] == "ok" and result["verified"] is True, result
    assert result["runtime"] == "local" and result["gate_trust"] == "local" and "on this machine" in result["runtime_reason"]
    assert len(local.calls) == 3 and vm.calls == []


# The real VM, opt-in


REMOTE = os.environ.get("FF_TEST_REMOTE") == "1"


@pytest.mark.docker
@pytest.mark.skipif(not REMOTE, reason="set FF_TEST_REMOTE=1 to use the real runner VM named in the owner's [remote] section")
def test_the_ticket_api_and_its_fix_on_the_real_runner_vm(tmp_path, project, monkeypatch) -> None:
    """Uses the owner's [remote] section and deploy record, read-only, with a state directory of
    its own; the credit floor is faked (the VM already bills by the hour whatever runs on it)."""
    real_config = Path(os.environ.get("FF_REAL_MCP_CONFIG", Path.home() / ".config" / "faultforge" / "mcp.toml"))
    real_state = Path(os.environ.get("FF_REAL_MCP_STATE", Path.home() / ".local" / "state" / "faultforge"))
    text = real_config.read_text(encoding="utf-8")
    remote_section = text[text.index("[remote]"):]
    config = tmp_path / "mcp.toml"
    config.write_text(f"allowed_roots = {json.dumps([str(project.parent)])}\n\n{remote_section}", encoding="utf-8")
    state = tmp_path / "state"
    state.mkdir()
    (state / "remote").mkdir()
    shutil.copyfile(real_state / "remote" / "deployed.json", state / "remote" / "deployed.json")
    monkeypatch.setenv(settings_module.CONFIG_ENV, str(config))
    monkeypatch.setenv(settings_module.STATE_ENV, str(state))
    guard, _, _, _ = guard_with(tmp_path, (-200.0, 0.0))
    tools = Tools(credit=guard)
    timings = {}
    started = time.monotonic()
    run = tools.run_scenario(str(project), SCENARIO, "worktree", runtime="remote")
    timings["run_scenario"] = round(time.monotonic() - started, 1)
    assert run["status"] == "ok" and run["runtime"] == "remote" and run["source_left_this_machine"] is True, run
    assert run["verdict"] == "FAIL" and run["containment"]["state_verification"] == "matched", run
    assert run["containment"]["limits_applied"] is True and run["containment"]["containers_left"] == 0
    fix_the_code(project)
    started = time.monotonic()
    fixed = tools.verify_fix(run["run_id"], runtime="remote")
    timings["verify_fix"] = round(time.monotonic() - started, 1)
    assert fixed["verified"] is True and all(rows(fixed).values()), fixed
    assert "runtime remote" in fixed["source"] and fixed["gate_trust"] == "remote-reported" and fixed["source_left_this_machine"] is True
    assert run["gate_trust"] == "remote-reported"
    record = remote_module.read_record(state)
    check = remote_module.ping_check(remote_module.static_check(settings_module.load_settings().remote, None, state, remote_module.SshTransport()))
    assert check.runtime is not None, check.reason
    answer = check.runtime.ping()
    assert answer["snapshots"] == 0 and answer["sandbox_containers"] == 0, answer
    print(f"\nREMOTE TIMINGS {json.dumps(timings)} deploy {record.name[:12]}")


@pytest.mark.docker
@pytest.mark.skipif(not REMOTE, reason="set FF_TEST_REMOTE=1 to use the real runner VM named in the owner's [remote] section")
def test_the_redeem_code_stack_built_from_source_on_the_real_runner_vm(tmp_path, monkeypatch) -> None:
    """The M11 demo, built from source on the VM: a cold install cache there fetches the lockfile from
    registry.npmjs.org through the VM's own egress proxy (allow_network_install), then the race FAILs,
    and the reference fix is verified through a rebuild and four stack runs, all on the VM."""
    from test_mcp import git

    demo = Path(__file__).resolve().parents[1] / "demo" / "redeem-code"
    project = tmp_path / "repo" / "app"
    shutil.copytree(demo, project, ignore=shutil.ignore_patterns("node_modules", "build", ".svelte-kit"))
    git(project.parent, "init", "-q")
    git(project.parent, "add", ".")
    git(project.parent, "commit", "-qm", "initial")
    real_config = Path(os.environ.get("FF_REAL_MCP_CONFIG", Path.home() / ".config" / "faultforge" / "mcp.toml"))
    real_state = Path(os.environ.get("FF_REAL_MCP_STATE", Path.home() / ".local" / "state" / "faultforge"))
    text = real_config.read_text(encoding="utf-8")
    config = tmp_path / "mcp.toml"
    config.write_text(f"allowed_roots = {json.dumps([str(project.parent)])}\nallow_network_install = true\n\n{text[text.index('[remote]'):]}", encoding="utf-8")
    state = tmp_path / "state"
    (state / "remote").mkdir(parents=True)
    shutil.copyfile(real_state / "remote" / "deployed.json", state / "remote" / "deployed.json")
    monkeypatch.setenv(settings_module.CONFIG_ENV, str(config))
    monkeypatch.setenv(settings_module.STATE_ENV, str(state))
    guard, _, _, _ = guard_with(tmp_path, (-200.0, 0.0))
    tools = Tools(credit=guard)
    timings = {}
    started = time.monotonic()
    run = tools.run_scenario(str(project), "same-code-race", "worktree", runtime="remote")
    timings["run_scenario"] = round(time.monotonic() - started, 1)
    assert run["status"] == "ok" and run["runtime"] == "remote" and run["verdict"] == "FAIL", run
    assert run["containment"]["state_verification"] == "matched" and run["containment"]["containers_left"] == 0, run
    timings["build"] = run["source"]["build"]
    shutil.copytree(demo / "reference_fix", project, dirs_exist_ok=True)
    started = time.monotonic()
    fixed = tools.verify_fix(run["run_id"], runtime="remote")
    timings["verify_fix"] = round(time.monotonic() - started, 1)
    assert fixed["status"] == "ok" and fixed["verified"] is True, fixed
    assert all("passed, PASS, database matched" in line for line in fixed["regression"]) and len(fixed["regression"]) == 3
    assert "runtime remote" in fixed["source"] and fixed["containers_left"] == 0 and fixed["gate_trust"] == "remote-reported"
    check = remote_module.ping_check(remote_module.static_check(settings_module.load_settings().remote, None, state, remote_module.SshTransport()))
    assert check.runtime is not None, check.reason
    answer = check.runtime.ping()
    assert answer["snapshots"] == 0 and answer["sandbox_containers"] == 0, answer
    print(f"\nREMOTE STACK TIMINGS {json.dumps(timings)}")


def test_each_step_sends_only_the_baseline_report_not_earlier_runs(monkeypatch, tmp_path, project) -> None:
    setup_remote(monkeypatch, tmp_path, project)
    vm, baseline = remote_baseline(tmp_path, project)
    fix_the_code(project)
    result = tools_for(tmp_path, vm).verify_fix(baseline["run_id"])
    assert result["verified"] is True
    sent = []
    for _, request, stdin in vm.calls[1:]:
        with tarfile.open(fileobj=io.BytesIO(stdin.split(b"\n", 1)[1])) as archive:
            sent.append(sorted({name.split("/")[3] for name in archive.getnames() if name.startswith("work/.faultforge/runs/") and name.count("/") >= 3}))
    # The main run gets the baseline's report; each regression run gets nothing of the earlier runs.
    assert sent == [[baseline["run_id"]], [], []]


def test_a_stack_image_the_deploy_lacks_keeps_auto_local(tmp_path) -> None:
    state = tmp_path / "state"
    remote_module.write_record(state, DeployRecord(host=HOST, user="runner", name=NAME, code_sha256=CODE, images=IMAGES, deployed_at="x"))
    keys = tmp_path / "k"
    keys.write_text("x")
    config = RemoteConfig(host=HOST, key_path=keys, known_hosts_path=keys)
    check = remote_module.static_check(
        config, None, state, FakeVM(tmp_path / "vm"), image_ids=lambda names: {name: IMAGES.get(name) for name in names},
        code_sha256=lambda: CODE, needs=("postgres:16-alpine",),
    )
    assert check.runtime is None and check.reason == "the deploy lacks postgres:16-alpine" and "deploy_runner.sh" in check.hint


def test_a_disk_error_putting_the_run_back_is_a_remote_failure(monkeypatch, tmp_path, project) -> None:
    state = setup_remote(monkeypatch, tmp_path, project)
    vm = FakeVM(tmp_path / "vm", run_sandboxed=Honest(Recorder()))
    real_replace = os.replace

    def failing(source, destination):
        if "/work/" in str(destination):
            raise OSError("disk full")
        return real_replace(source, destination)

    monkeypatch.setattr(remote_module.os, "replace", failing)
    result = tools_for(tmp_path, vm).run_scenario(str(project), SCENARIO, "worktree", runtime="remote")
    assert result["error_class"] == "sandbox_failed" and "couldn't be put in place" in result["message"]
    assert not (state / "runs").exists() or list((state / "runs").iterdir()) == []


def _rebuild(completed: Completed, change) -> dict:
    """The reviewer's tamper: rewrite the returned tar member by member, keeping the answer line."""
    head, _, rest = completed.stdout.partition(b"\n")
    out = io.BytesIO()
    with tarfile.open(fileobj=io.BytesIO(rest)) as source, tarfile.open(fileobj=out, mode="w") as target:
        for member in source:
            data = source.extractfile(member).read() if member.isreg() else None
            data = change(member, data, target)
            if data is not None:
                member.size = len(data)
                target.addfile(member, io.BytesIO(data))
            else:
                target.addfile(member)
    archive = out.getvalue()
    return {"stdout": json.dumps({**json.loads(head), "tar_bytes": len(archive)}).encode() + b"\n" + archive}


def test_the_stored_report_is_the_validated_answer_never_the_tars_copy(monkeypatch, tmp_path, project) -> None:
    state = setup_remote(monkeypatch, tmp_path, project)

    def change(member, data, target):
        if member.name.endswith("/report.json"):
            report = json.loads(data)
            report["verdict_reason"] = "FORGED ON DISK ONLY"
            return json.dumps(report).encode()
        return data

    vm = TamperingTransport(tmp_path / "vm", lambda completed: _rebuild(completed, change), run_sandboxed=Honest(Recorder()))
    result = tools_for(tmp_path, vm).run_scenario(str(project), SCENARIO, "worktree", runtime="remote")
    assert result["status"] == "ok" and "FORGED" not in result["verdict_reason"], result
    stored = store.run_path(state, result["run_id"])
    assert b"FORGED" not in (stored / "report.json").read_bytes() and b"FORGED" not in (stored / "report.md").read_bytes()


@pytest.mark.parametrize("planted", ["pinned/planted.txt", "run.json", "trials/1/evil.sh", "../run_20991231_000000_ffff/report.json"])
def test_a_planted_name_refuses_the_whole_archive_before_anything_is_written(monkeypatch, tmp_path, project, planted) -> None:
    state = setup_remote(monkeypatch, tmp_path, project)

    def change(member, data, target):
        if member.name.endswith("/report.json"):
            extra = tarfile.TarInfo(member.name.rsplit("/", 1)[0] + "/" + planted)
            extra.size = 3
            target.addfile(extra, io.BytesIO(b"abc"))
        return data

    vm = TamperingTransport(tmp_path / "vm", lambda completed: _rebuild(completed, change), run_sandboxed=Honest(Recorder()))
    result = tools_for(tmp_path, vm).run_scenario(str(project), SCENARIO, "worktree", runtime="remote")
    assert result["status"] == "error" and result["error_class"] == "sandbox_failed", result
    assert result["source_left_this_machine"] is True
    left = sorted(path.relative_to(state).as_posix() for path in (state / "runs").rglob("*")) if (state / "runs").exists() else []
    assert left == [] and list((state / "work").iterdir()) == []


def test_a_dot_member_is_skipped_and_a_dot_file_refused(tmp_path) -> None:
    remote_run.unpack(io.BytesIO(_tar(("dir", ".", b""), ("file", "work/a", b"x"))), tmp_path, 10, ("work",))
    assert (tmp_path / "work" / "a").read_bytes() == b"x"
    with pytest.raises(remote_run.Refused):
        remote_run.unpack(io.BytesIO(_tar(("file", ".", b"x"))), tmp_path / "w", 10, ("work",))
