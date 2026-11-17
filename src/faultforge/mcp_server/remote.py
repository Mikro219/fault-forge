"""The remote runtime, on the owner's machine (docs/PLAN.md V4.2, V4.3, V4.5 and V4.6, M12).

A run in remote mode is made exactly as a local one: the snapshot is taken here (the allowlist, the
secret-looking files dropped, the pinning), and so are every decision and every stored file. Only the
sandboxed steps move: `run_sandboxed`, `run_stack`, `build_stack_image` and `remove_images` are each
one SSH call to the runner VM's one entry point, `faultforge.remote_run`, with a JSON request and a
tar of the step's inputs on stdin. The answer and a tar of the run directory come back on stdout and
are checked here like a sandbox's build output, before anything is stored.

What leaves this machine: the snapshot of the project (the files a local run would mount), the pinned
config and the step's settings. Never a key: the inference and Vultr keys stay here, a request carries
none, and the ssh process gets a minimal environment with no agent socket.

SSH is always the same: the owner's runner key only, the pinned host key only (StrictHostKeyChecking
yes, a known_hosts file of its own), BatchMode, no user or system ssh config, no agent, no forwarding,
connect and keepalive timeouts, and a hard deadline per call. The remote command is a fixed path built
from constants and the deploy record's hex commit, checked against a strict pattern: no project text
is ever on a command line.
"""

import dataclasses
import io
import json
import math
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Protocol

from pydantic import Field

from faultforge import node_build, remote_run, report as report_module, sandbox, stack as stack_module
from faultforge.errors import FaultForgeError
from faultforge.lifecycle import RunOptions
from faultforge.models import Contract, Report
from faultforge.sandbox import SandboxResult, TargetSpec

DEFAULT_USER = "runner"
DEFAULT_HOURLY_USD = 0.066
DEFAULT_KEY = "~/.ssh/faultforge_vultr"
DEFAULT_KNOWN_HOSTS = "~/.config/faultforge/runner_known_hosts"
REMOTE_KEYS = {"host", "user", "key_path", "known_hosts_path", "hourly_usd", "auto"}
HOST = re.compile(r"[0-9]{1,3}(\.[0-9]{1,3}){3}|[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)+")
USER = re.compile(r"[a-z_][a-z0-9_-]{0,31}")
PLAIN_PATH = re.compile(r"/[A-Za-z0-9._/-]{1,512}")
DEPLOY_NAME = re.compile(r"[a-f0-9]{40}|[a-f0-9]{64}")
COMMAND_TOKEN = re.compile(r"[A-Za-z0-9/._-]{1,256}")
RECORD_DIR = "remote"
RECORD_NAME = "deployed.json"
MAX_RECORD_BYTES = 64 * 1024
MAX_ANSWER_BYTES = 48 * 1024 * 1024
MAX_STDERR_BYTES = 16 * 1024
CONNECT_TIMEOUT_S = 10
ALIVE_INTERVAL_S = 15
ALIVE_COUNT = 4
PING_TIMEOUT_S = 25.0
TRANSFER_MARGIN_S = 120.0
SSH_ENV_NAMES = ("PATH", "HOME")
DEPLOY_HINT = "Run scripts/deploy_runner.sh HEAD (it deploys a commit), then call the tool again. Use runtime \"local\" meanwhile."
LEFT_THIS_MACHINE = (
    "Remote runtime: the snapshot of the project left this machine over SSH for the runner VM {host} and ran there "
    "in the same sandbox. Each step's copy was removed there when the step ended; an image built from it is removed "
    "at the end of the call, or swept on the VM within two hours if that fails. No key went with it."
)
STAYED = "Local runtime: nothing left this machine."


def required_images() -> tuple[str, ...]:
    """What every deploy must carry. A stack's toolchain and stock database images are carried too
    when the deploy had them, and a build or run without them fails there like a local one."""
    policy = sandbox.DEFAULT_POLICY
    return (policy.target_image, policy.runner_image, stack_module.HOOKS_IMAGE, node_build.PROXY_IMAGE)


# The [remote] section of mcp.toml


@dataclass(frozen=True)
class RemoteConfig:
    host: str
    user: str = DEFAULT_USER
    key_path: Path = Path(DEFAULT_KEY).expanduser()
    known_hosts_path: Path = Path(DEFAULT_KNOWN_HOSTS).expanduser()
    hourly_usd: float = DEFAULT_HOURLY_USD
    # What runtime "auto" prefers when every condition for remote holds: "remote" (the default) or "local".
    auto: str = "remote"


def _plain_path(raw: object, key: str, default: str) -> Path:
    if raw is None:
        raw = default
    if not isinstance(raw, str):
        raise ValueError(f"[remote] {key} must be a path")
    path = Path(raw).expanduser()
    if not PLAIN_PATH.fullmatch(str(path)):
        raise ValueError(f"[remote] {key} must be an absolute path of plain characters")
    return path


def parse_remote(raw: object) -> RemoteConfig:
    """Raises ValueError with fixed text, which says which key is wrong."""
    if not isinstance(raw, dict):
        raise ValueError("[remote] must be a table")
    unknown = set(raw) - REMOTE_KEYS
    if unknown:
        raise ValueError(f"[remote] has unknown keys {sorted(unknown)}")
    host = raw.get("host")
    if not isinstance(host, str) or not HOST.fullmatch(host):
        raise ValueError("[remote] host must be an IPv4 address or a lowercase host name")
    user = raw.get("user", DEFAULT_USER)
    if not isinstance(user, str) or not USER.fullmatch(user) or user == "root":
        raise ValueError("[remote] user must be a plain user name, not root")
    rate = raw.get("hourly_usd", DEFAULT_HOURLY_USD)
    if type(rate) not in (int, float) or not math.isfinite(rate) or not 0 < rate <= 10:
        raise ValueError("[remote] hourly_usd must be a number above 0 and at most 10")
    auto = raw.get("auto", "remote")
    if auto not in ("remote", "local"):
        raise ValueError('[remote] auto must be "remote" or "local"')
    return RemoteConfig(
        host=host, user=user, key_path=_plain_path(raw.get("key_path"), "key_path", DEFAULT_KEY),
        known_hosts_path=_plain_path(raw.get("known_hosts_path"), "known_hosts_path", DEFAULT_KNOWN_HOSTS),
        hourly_usd=float(rate), auto=auto,
    )


# The deploy record, written by scripts/deploy_runner.sh through `record` below


class DeployRecord(Contract):
    host: str
    user: str
    name: str = Field(pattern=r"^([a-f0-9]{40}|[a-f0-9]{64})$", description="The app directory on the VM: a commit, or a tree for --worktree")
    commit: str | None = Field(default=None, pattern=r"^[a-f0-9]{40}$")
    code_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    images: dict[str, str] = Field(max_length=32)
    uv: str | None = None
    deployed_at: str


def record_path(state: Path) -> Path:
    return state / RECORD_DIR / RECORD_NAME


def read_record(state: Path) -> DeployRecord | None:
    path = record_path(state)
    try:
        info = path.lstat()
        if not path.is_file() or path.is_symlink() or info.st_size > MAX_RECORD_BYTES:
            return None
        return DeployRecord.model_validate_json(path.read_bytes())
    except (OSError, ValueError):
        return None


def write_record(state: Path, record: DeployRecord) -> Path:
    path = record_path(state)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    descriptor = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as out:
        out.write(record.model_dump_json(indent=2))
    os.replace(temp, path)
    return path


# SSH


def remote_command(name: str) -> list[str]:
    """The one command ever run on the VM. name is the deploy record's hex commit or tree."""
    if not DEPLOY_NAME.fullmatch(name):
        raise ValueError("not a deploy name")
    command = [f"{remote_run.BASE}/app/{name}/.venv/bin/python", "-I", "-m", "faultforge.remote_run"]
    if not all(COMMAND_TOKEN.fullmatch(token) for token in command):
        raise ValueError("a command token isn't plain")
    return command


def ssh_argv(config: RemoteConfig, command: list[str]) -> list[str]:
    """ssh joins the command into one string for the VM's shell, so every token must be plain; they
    are constants and a hex name, never project text."""
    if not all(COMMAND_TOKEN.fullmatch(token) for token in command):
        raise ValueError("a command token isn't plain")
    options = {
        "BatchMode": "yes",
        "StrictHostKeyChecking": "yes",
        "UserKnownHostsFile": str(config.known_hosts_path),
        "GlobalKnownHostsFile": "/dev/null",
        "UpdateHostKeys": "no",
        "IdentitiesOnly": "yes",
        "IdentityAgent": "none",
        "PasswordAuthentication": "no",
        "KbdInteractiveAuthentication": "no",
        "ForwardAgent": "no",
        "ForwardX11": "no",
        "ClearAllForwardings": "yes",
        "RequestTTY": "no",
        "ConnectTimeout": str(CONNECT_TIMEOUT_S),
        "ServerAliveInterval": str(ALIVE_INTERVAL_S),
        "ServerAliveCountMax": str(ALIVE_COUNT),
        "LogLevel": "ERROR",
    }
    argv = ["ssh", "-F", "/dev/null"]
    for key, value in options.items():
        argv += ["-o", f"{key}={value}"]
    return [*argv, "-i", str(config.key_path), "-l", config.user, "--", config.host, *command]


def ssh_env() -> dict[str, str]:
    """No agent socket, no keys, nothing but where ssh is and the home it would look in."""
    env = {name: os.environ[name] for name in SSH_ENV_NAMES if name in os.environ}
    env.setdefault("PATH", sandbox.DEFAULT_PATH)
    env["LC_ALL"] = "C"
    return env


@dataclass(frozen=True)
class Completed:
    returncode: int | None
    stdout: bytes
    stderr: bytes
    timed_out: bool = False
    overflowed: bool = False


class Transport(Protocol):
    def call(self, argv: list[str], stdin: bytes, timeout_s: float, max_stdout: int) -> Completed: ...


class SshTransport:
    """Runs ssh with a deadline and a cap on what it may print. The process group is killed on a
    timeout or an overflow, so nothing of it outlives the call here."""

    def call(self, argv: list[str], stdin: bytes, timeout_s: float, max_stdout: int) -> Completed:
        # A bare "ssh" is resolved here against the fixed system PATH, never the caller's, and must
        # be an absolute path: a planted ssh earlier on a user PATH is never run.
        if argv and argv[0] == "ssh":
            found = shutil.which("ssh", path=sandbox.DEFAULT_PATH)
            if found is None or not os.path.isabs(found):
                raise FileNotFoundError("ssh")
            argv = [found, *argv[1:]]
        process = subprocess.Popen(
            argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=ssh_env(), start_new_session=True,
        )
        out, err = bytearray(), bytearray()
        overflow = threading.Event()

        def write() -> None:
            try:
                process.stdin.write(stdin)
            except (BrokenPipeError, OSError):
                pass
            finally:
                try:
                    process.stdin.close()
                except OSError:
                    pass

        def read(stream, into: bytearray, cap: int, flag: threading.Event | None) -> None:
            while True:
                chunk = stream.read(65536)
                if not chunk:
                    return
                if len(into) + len(chunk) > cap:
                    if flag is not None:
                        flag.set()
                        _kill(process)
                    return
                into.extend(chunk)

        threads = [
            threading.Thread(target=write, daemon=True),
            threading.Thread(target=read, args=(process.stdout, out, max_stdout, overflow), daemon=True),
            threading.Thread(target=read, args=(process.stderr, err, MAX_STDERR_BYTES, None), daemon=True),
        ]
        for thread in threads:
            thread.start()
        timed_out = False
        try:
            process.wait(timeout=timeout_s)
        except subprocess.TimeoutExpired:
            timed_out = True
            _kill(process)
            process.wait()
        for thread in threads:
            thread.join(timeout=5)
        return Completed(process.returncode, bytes(out), bytes(err), timed_out, overflow.is_set())


def _kill(process: subprocess.Popen) -> None:
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


# The runtime


class RemoteFailure(FaultForgeError):
    """A remote step that didn't give a usable answer. Nothing it returned is used."""

    def __init__(self, message: str):
        super().__init__(
            "sandbox_failed", f"the remote runtime failed: {message}",
            "Nothing was stored from this step. Call faultforge_status to check the runner VM, or use runtime \"local\".",
        )


def _clean(text: object, limit: int = 200) -> str:
    return sandbox._clean(str(text), limit)


def local_image_ids(names: list[str]) -> dict[str, str | None]:
    """None for an image Docker can't describe, so a Docker that fails or hangs reads as a mismatch."""
    found: dict[str, str | None] = {}
    for name in names:
        try:
            found[name] = node_build._image_id(node_build.POLICY, name)
        except sandbox.SandboxError:
            found[name] = None
    return found


def wall_s(op: str) -> float:
    """The hard deadline of one call from here: the VM's own deadline for the step plus the transfer."""
    return PING_TIMEOUT_S if op == "ping" else remote_run.op_deadline_s(op) + TRANSFER_MARGIN_S


def worst_case_usd(config: RemoteConfig, ops: Mapping[str, int]) -> tuple[Decimal, float]:
    """docs/PLAN.md V4.3: the VM's hourly rate times the call's wall limit, rounded up to whole hours.
    The wall limit is the sum of every step's deadline the call can make, and a ping."""
    from faultforge.mcp_server import credit

    total = wall_s("ping") + sum(wall_s(op) * count for op, count in ops.items())
    return credit.remote_run_worst_case_usd(config.hourly_usd, total), total


class RemoteRuntime:
    """The four sandboxed steps, each one SSH call. Kept for one tool call."""

    name = "remote"

    def __init__(self, config: RemoteConfig, record: DeployRecord, code_sha256: str, transport: Transport):
        self.config = config
        self.record = record
        self.code_sha256 = code_sha256
        self.transport = transport
        self.failure: RemoteFailure | None = None
        self.calls = 0
        # Whether any step's request, so the project's snapshot, was handed to ssh during this call.
        self.sent = False

    # One call

    def _call(self, request: dict[str, Any], inputs: bytes, op: str) -> tuple[dict[str, Any], bytes]:
        try:
            return self._exchange(request, inputs, op)
        except RemoteFailure as error:
            self.failure = error
            raise

    def _exchange(self, request: dict[str, Any], inputs: bytes, op: str) -> tuple[dict[str, Any], bytes]:
        self.calls += 1
        argv = ssh_argv(self.config, remote_command(self.record.name))
        line = json.dumps(request, separators=(",", ":")).encode("utf-8")
        if len(line) > remote_run.MAX_REQUEST_BYTES:
            raise RemoteFailure("the request is too large")
        if op != "ping":
            self.sent = True
        try:
            completed = self.transport.call(argv, line + b"\n" + inputs, wall_s(op), MAX_ANSWER_BYTES + remote_run.MAX_OUTPUT_BYTES)
        except OSError as error:
            raise RemoteFailure(f"ssh couldn't be started ({type(error).__name__})") from None
        if completed.timed_out:
            raise RemoteFailure(f"no answer within {wall_s(op):.0f} s")
        if completed.overflowed:
            raise RemoteFailure("the VM sent more than the cap")
        head, newline, rest = completed.stdout.partition(b"\n")
        if not newline:
            reason = "the VM couldn't be reached" if completed.returncode == 255 else "the VM sent no answer"
            raise RemoteFailure(reason)
        if len(head) > MAX_ANSWER_BYTES:
            raise RemoteFailure("the answer is too large")
        try:
            answer = json.loads(head)
        except (ValueError, RecursionError):
            raise RemoteFailure("the answer isn't JSON") from None
        if not isinstance(answer, dict):
            raise RemoteFailure("the answer isn't an object")
        size = answer.get("tar_bytes")
        if type(size) is not int or size < 0 or size != len(rest):
            raise RemoteFailure("the answer's archive is incomplete")
        if answer.get("ok") is not True:
            raise RemoteFailure(f"the VM refused: {_clean(answer.get('error', 'no reason'))}")
        if "snapshot_removed" in answer and answer["snapshot_removed"] is not True:
            raise RemoteFailure("the VM couldn't remove the step's directory")
        return answer, rest

    def _expect(self) -> dict[str, Any]:
        return {"code_sha256": self.code_sha256, "images": dict(self.record.images)}

    def ping(self) -> dict[str, Any]:
        answer, _ = self._call({"op": "ping", "images": list(self.record.images)}, b"", "ping")
        return answer

    # Results come back as a tar of the run directory, checked like a build's output.

    def _restore(self, archive: bytes, project_dir: Path, report: Report | None) -> None:
        """Only the run the answer names comes back, and only the names a run writes; anything else
        (a planted pinned/, a second run, a stray file) refuses the whole archive before any of it
        reaches the run directory. report.json and report.md are then written here from the
        validated answer, so the stored report is the one the gate read, never the tar's copy."""
        project_dir = Path(project_dir)
        if report is None:
            if archive:
                raise RemoteFailure("a failed run sent a run directory back")
            return
        with tempfile.TemporaryDirectory(prefix=".remote-", dir=project_dir.parent) as temp:
            landing = Path(temp)
            try:
                remote_run.unpack(
                    io.BytesIO(archive), landing, remote_run.MAX_OUTPUT_BYTES, ("work",), allow=remote_run.run_dir_member(report.run_id),
                )
            except (remote_run.Refused, OSError, ValueError, EOFError) as error:
                raise RemoteFailure(f"the run directory that came back was refused ({_clean(error, 80)})") from None
            except Exception as error:  # tarfile's own errors
                raise RemoteFailure(f"the run directory that came back was refused ({type(error).__name__})") from None
            returned = landing / "work"
            run = returned / ".faultforge" / "runs" / report.run_id
            if not run.is_dir():
                raise RemoteFailure("no run directory came back")
            try:
                for name in (report_module.REPORT_NAME, report_module.SUMMARY_NAME):
                    (run / name).unlink(missing_ok=True)
                report_module.write_report(run, report)
            except OSError as error:
                raise RemoteFailure(f"the run directory couldn't be put in place ({type(error).__name__})") from None
            # Merged over the run directory: only this step's run (and the baseline report it was
            # sent) came back, and a file that already exists here is replaced by the VM's copy.
            try:
                for directory, _, files in os.walk(returned):
                    relative = Path(directory).relative_to(returned)
                    (project_dir / relative).mkdir(mode=0o700, parents=True, exist_ok=True)
                    for name in files:
                        os.replace(Path(directory) / name, project_dir / relative / name)
            except OSError as error:
                raise RemoteFailure(f"the run directory couldn't be put in place ({type(error).__name__})") from None

    @staticmethod
    def _result(answer: dict[str, Any]) -> SandboxResult:
        try:
            return SandboxResult.model_validate(answer.get("result"))
        except ValueError:
            raise RemoteFailure("the VM's result isn't a valid sandbox result") from None

    # The four steps, with the local signatures

    def run_sandboxed(self, spec: TargetSpec, project_dir: Path, scenario_id: str, options: RunOptions = RunOptions()) -> SandboxResult:
        if spec.allow_hostile:
            raise RemoteFailure("the hostile demo's opt-in never runs remotely")
        # What the VM must report as deployed: the same files written the same way, hashed here.
        code_dir = sandbox.write_code_dir(spec)
        try:
            expected = sandbox.code_dir_sha256(code_dir)
        finally:
            shutil.rmtree(code_dir, ignore_errors=True)
        support = Path(spec.support_dir) if spec.support_dir is not None else None
        entries: dict[str, Path | bytes] = {"app.py": spec.app_source, "config/faultforge.yaml": Path(spec.config_path), **self._work(project_dir, options)}
        if support is not None:
            entries[f"support/{support.name}"] = support
        inputs = self._pack(entries)
        request = {
            "op": "run_python", "scenario_id": scenario_id, "options": dataclasses.asdict(options),
            "support": support.name if support is not None else None,
            "state_verifier": dict(spec.state_verifier) if spec.state_verifier is not None else None,
            "expect": self._expect(),
        }
        answer, archive = self._call(request, inputs, "run_python")
        result = self._result(answer)
        if result.deployed_code_sha256 is not None and result.deployed_code_sha256 != expected:
            raise self._fail("the VM deployed other code than was sent")
        self._restore_or_fail(archive, project_dir, result.report)
        return result

    def run_stack(self, spec: stack_module.StackSpec, project_dir: Path, scenario_id: str, options: RunOptions = RunOptions()) -> SandboxResult:
        inputs = self._pack({"config/faultforge.yaml": Path(spec.config_path), **self._work(project_dir, options)})
        request = {
            "op": "run_stack", "scenario_id": scenario_id, "options": dataclasses.asdict(options),
            "app_image": spec.app_image, "code_sha256": spec.code_sha256, "stack": spec.stack.model_dump(mode="json"),
            "expect": self._expect(),
        }
        answer, archive = self._call(request, inputs, "run_stack")
        result = self._result(answer)
        if spec.code_sha256 is not None and result.deployed_code_sha256 not in (None, spec.code_sha256):
            raise self._fail("the VM reported other code than was built")
        self._restore_or_fail(archive, project_dir, result.report)
        return result

    def build_stack(self, source: Path, build: node_build.BuildConfig, state: Path, *, allow_fetch: bool) -> node_build.StackBuild:
        """state is this machine's and isn't sent: the VM keeps its install cache in its own fixed one."""
        expected = node_build.source_sha256(Path(source))
        inputs = self._pack({"source": Path(source)})
        request = {"op": "build_stack", "build": build.model_dump(mode="json"), "allow_fetch": allow_fetch, "expect": self._expect()}
        answer, _ = self._call(request, inputs, "build_stack")
        built = _stack_build(answer.get("build"))
        if built is None:
            raise self._fail("the VM's build result isn't valid")
        if built.image_id is not None and built.source_sha256 != expected:
            raise self._fail("the VM built other source than was sent")
        return built

    def remove_images(self, images: list[str]) -> None:
        """Never raises: like the local one, a leftover is swept at the next build by its label."""
        ids = [image for image in images if remote_run.IMAGE_ID.fullmatch(image)]
        if not ids:
            return
        try:
            self._call({"op": "remove_images", "images": ids, "expect": self._expect()}, b"", "remove_images")
        except RemoteFailure:
            pass

    def _pack(self, entries: Mapping[str, Path | bytes]) -> bytes:
        try:
            return remote_run.pack(entries)
        except (remote_run.Refused, OSError) as error:
            raise self._fail(f"the inputs couldn't be packed ({_clean(error, 80)})") from None

    @staticmethod
    def _work(project_dir: Path, options: RunOptions) -> dict[str, Path]:
        """What of the run directory a step needs: only the baseline's report for a comparison. Earlier
        runs of the same call stay here, so a verify's transfers don't grow with each regression run."""
        entries: dict[str, Path] = {}
        if options.compare_to is not None:
            baseline = Path(project_dir) / ".faultforge" / "runs" / options.compare_to
            if baseline.is_dir() and not baseline.is_symlink():
                entries[f"work/.faultforge/runs/{options.compare_to}"] = baseline
        return entries

    def _restore_or_fail(self, archive: bytes, project_dir: Path, report: Report | None) -> None:
        try:
            self._restore(archive, project_dir, report)
        except RemoteFailure as error:
            self.failure = error
            raise

    def _fail(self, message: str) -> RemoteFailure:
        self.failure = RemoteFailure(message)
        return self.failure


_BUILD_FIELDS = {item.name: item for item in dataclasses.fields(node_build.StackBuild)}


def _stack_build(raw: object) -> node_build.StackBuild | None:
    if not isinstance(raw, dict) or set(raw) != set(_BUILD_FIELDS):
        return None
    image_id = raw["image_id"]
    if image_id is not None and (not isinstance(image_id, str) or not remote_run.IMAGE_ID.fullmatch(image_id)):
        return None
    for name in ("image", "source_sha256", "failure_reason", "failure_detail", "log_tail", "install", "cache_key"):
        if raw[name] is not None and not isinstance(raw[name], str):
            return None
    if not isinstance(raw["proxy"], list) or not all(isinstance(item, str) for item in raw["proxy"]):
        return None
    if not isinstance(raw["seconds"], dict) or not all(isinstance(key, str) and type(value) in (int, float) for key, value in raw["seconds"].items()):
        return None
    if not isinstance(raw["toolchain"], dict) or not all(isinstance(key, str) and isinstance(value, str) for key, value in raw["toolchain"].items()):
        return None
    if raw["build_peak_mb"] is not None and type(raw["build_peak_mb"]) is not int:
        return None
    return node_build.StackBuild(**{**raw, "proxy": tuple(raw["proxy"])})


# Whether the remote runtime can be used now


@dataclass
class Check:
    runtime: RemoteRuntime | None
    reason: str | None
    hint: str = ""


def static_check(
    config: RemoteConfig | None, problem: str | None, state: Path, transport: Transport,
    *, image_ids: Callable[[list[str]], dict[str, str | None]] = local_image_ids, code_sha256: Callable[[], str] = remote_run.package_sha256,
    needs: tuple[str, ...] = (),
) -> Check:
    """Everything that needs no network: the config, the deploy record, and that the record matches
    this machine's code and images."""
    if config is None:
        return Check(None, problem or "no [remote] section in mcp.toml", "Add a [remote] section to mcp.toml (docs/MCP.md, \"The remote runtime\").")
    record = read_record(state)
    if record is None:
        return Check(None, "FaultForge isn't deployed to the runner VM", DEPLOY_HINT)
    if (record.host, record.user) != (config.host, config.user):
        return Check(None, "the deploy record is for another host or user", DEPLOY_HINT)
    # needs: the images this call's stack names (its toolchain, postgres, redis). Each must be in the
    # record, so it is compared with this machine's below and checked again by the VM on every request.
    missing = [name for name in (*required_images(), *needs) if name not in record.images]
    if missing:
        return Check(None, f"the deploy lacks {', '.join(missing)}", DEPLOY_HINT)
    local = code_sha256()
    if record.code_sha256 != local:
        return Check(None, f"the deployed FaultForge ({record.name[:12]}) isn't the code on this machine", DEPLOY_HINT)
    mine = image_ids(list(record.images))
    different = [name for name, identifier in record.images.items() if mine.get(name) != identifier]
    if different:
        return Check(None, f"the deployed images aren't this machine's ({', '.join(different[:3])})", DEPLOY_HINT)
    for path in (config.key_path, config.known_hosts_path):
        if not path.is_file():
            return Check(None, f"{path} doesn't exist", "Check key_path and known_hosts_path in [remote].")
    return Check(RemoteRuntime(config, record, local, transport), None)


def ping_check(check: Check) -> Check:
    """One SSH call: the VM answers, and its own code hash and image IDs are the record's."""
    runtime = check.runtime
    if runtime is None:
        return check
    try:
        answer = runtime.ping()
    except RemoteFailure as error:
        runtime.failure = None
        return Check(None, f"the runner VM didn't answer ({error.message.removeprefix('the remote runtime failed: ')})", "Check that the VM is running and the deploy record is current.")
    if answer.get("code_sha256") != runtime.code_sha256:
        return Check(None, "the runner VM runs other FaultForge code than its deploy record says", DEPLOY_HINT)
    if answer.get("images") != dict(runtime.record.images):
        return Check(None, "the runner VM's images aren't the deployed ones", DEPLOY_HINT)
    runtime.calls = 0
    return check


# The deploy helper for scripts/deploy_runner.sh


def _cli(argv: list[str]) -> int:
    """`config`: prints host, user, key and known_hosts, one per line, for the deploy script.
    `secrets`: reads NUL-separated paths on stdin and prints those snapshot.is_secret drops, exiting
    1 if there are any, so the deploy refuses before git writes a blob.
    `record <name> [commit] [uv] <image>...`: pings the deployed app and writes the record from what
    the VM itself reports, then says whether it matches this machine."""
    from faultforge.mcp_server.settings import load_settings
    from faultforge.mcp_server.snapshot import is_secret

    if argv[:1] == ["secrets"]:
        names = [name for name in sys.stdin.buffer.read().decode("utf-8", "surrogateescape").split("\0") if name]
        found = [name for name in names if is_secret(name)]
        for name in found:
            print(name, file=sys.stderr)
        return 1 if found else 0
    settings = load_settings()
    config = settings.remote
    if config is None:
        print(f"no usable [remote] section in {settings.config_path}: {settings.remote_problem}", file=sys.stderr)
        return 2
    if argv[:1] == ["config"]:
        print(config.host, config.user, config.key_path, config.known_hosts_path, sep="\n")
        return 0
    if len(argv) < 4 or argv[0] != "record":
        print("usage: python -m faultforge.mcp_server.remote config | record <name> <commit|-> <uv version> <image>...", file=sys.stderr)
        return 2
    name, commit, uv, images = argv[1], argv[2], argv[3], argv[4:]
    if not DEPLOY_NAME.fullmatch(name) or (commit != "-" and not re.fullmatch(r"[a-f0-9]{40}", commit)):
        print("name and commit must be hex object names", file=sys.stderr)
        return 2
    if not images or not all(remote_run.IMAGE_NAME.fullmatch(image) for image in images):
        print("image names must be plain name:tag", file=sys.stderr)
        return 2
    probe = DeployRecord(
        host=config.host, user=config.user, name=name, commit=None if commit == "-" else commit, code_sha256="0" * 64,
        images={image: "" for image in images}, uv=uv, deployed_at="",
    )
    runtime = RemoteRuntime(config, probe, "", SshTransport())
    try:
        answer = runtime.ping()
    except RemoteFailure as error:
        print(error.message, file=sys.stderr)
        return 1
    remote_images = answer.get("images")
    if not isinstance(remote_images, dict) or any(not isinstance(remote_images.get(image), str) for image in images):
        print(f"the VM lacks images: {[image for image in images if not isinstance((remote_images or {}).get(image), str)]}", file=sys.stderr)
        return 1
    code = answer.get("code_sha256")
    if not isinstance(code, str) or not re.fullmatch(r"[a-f0-9]{64}", code):
        print("the VM reported no code hash", file=sys.stderr)
        return 1
    record = DeployRecord(
        host=config.host, user=config.user, name=name, commit=probe.commit, code_sha256=code,
        images={image: remote_images[image] for image in images}, uv=uv, deployed_at=datetime.now(UTC).isoformat(timespec="seconds"),
    )
    path = write_record(settings.state_dir, record)
    local_images = local_image_ids(images)
    print(f"recorded {path}")
    print(f"code {code[:16]}: {'matches this machine' if code == remote_run.package_sha256() else 'DIFFERS from this machine (remote runs will be refused)'}")
    for image in images:
        same = local_images.get(image) == record.images[image]
        print(f"image {image} {record.images[image][:19]}: {'matches' if same else 'DIFFERS'}")
    return 0


if __name__ == "__main__":
    sys.exit(_cli(sys.argv[1:]))
