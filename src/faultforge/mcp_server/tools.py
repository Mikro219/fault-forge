"""The MCP tools as plain functions, so they can be tested without a transport (docs/PLAN.md V4.4).

Every tool returns a dict, and every failure is `{status: "error", error_class, message, fix_hint}`.
Nothing here reads an inference key, makes a network call or spends money. The only containers it
can touch are the sandbox's own, through sandbox.py and stack.py, which act only on containers that
carry the faultforge.sandbox label.

Targets:
- **python**: a single-container ASGI app, `app.py` at the project root plus at most one package
  beside it, run by `run_sandboxed` from a git snapshot (`worktree` or `commit:<sha>`).
- **stack**: a project with `stack.yaml`, run by `run_stack` from the prebuilt image that stack.yaml
  names (`source: "image"`). Building a stack from source arrives in M11.

A project's stack.yaml is treated as untrusted here, unlike in the repo's own demos: the trusted
services must be the stock postgres and redis images, and nothing may run as root.
"""

import contextlib
import dataclasses
import fcntl
import os
import re
import secrets
import stat
import threading
import time
from collections.abc import Callable, Iterator
from functools import wraps
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from faultforge import config as config_module, expressions, k6_runner, safety, sandbox, stack as stack_module
from faultforge.errors import FaultForgeError
from faultforge.lifecycle import RunOptions
from faultforge.limits import LIMITS
from faultforge.mcp_server import snapshot as snapshot_module, store
from faultforge.mcp_server.settings import McpSettings, load_settings, private_dir, require_image_allowed, resolve_project
from faultforge.models import ProjectConfig
from faultforge.sandbox import SandboxResult, TargetSpec
from faultforge.stack import HOOKS_IMAGE, StackConfig, StackSpec

STACK_NAME = "stack.yaml"
APP_NAME = "app.py"
MAX_STACK_BYTES = 256_000
# A page of list_scenarios stays within about 60 lines; the rest is paged with offset.
MAX_SCENARIOS_LISTED = 6
MAX_SCENARIOS_TOTAL = 10_000
RUN_LOCK_NAME = "run.lock"
SCENARIO_ID = re.compile(r"[a-z0-9][a-z0-9-]{0,63}")
POSTGRES_IMAGE = re.compile(r"postgres:[A-Za-z0-9._-]{1,64}")
REDIS_IMAGE = re.compile(r"redis:[A-Za-z0-9._-]{1,64}")
APP_IMAGE = re.compile(r"[a-z0-9][a-z0-9._/-]{0,127}(:[A-Za-z0-9._-]{1,128})?")
ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}")
WRITABLE_PATH = re.compile(r"/[A-Za-z0-9._/-]{1,128}")
RESERVED_PORTS = (5432, 6379)
STATUS_POLICY = dataclasses.replace(sandbox.DEFAULT_POLICY, docker_timeout_s=5.0)
REMOTE_UNAVAILABLE = "not built yet (M12)"
FAILURE_HINTS = {
    "docker_unavailable": "Start Docker, then call faultforge_status.",
    "image_missing": "Build the sandbox images with scripts/build_images.sh, and a stack's app image first.",
    "invalid_spec": "Fix the project's faultforge.yaml; faultforge_validate_scenario shows what's wrong.",
    "target_start_failed": "The target couldn't start in the sandbox. It must serve `uvicorn app:app` with its hooks.",
    "target_exited": "The target exited in the sandbox. Check that app.py imports and starts without network.",
    "target_unreachable": "The target never answered its health hook inside the sandbox.",
    "wall_limit_exceeded": "The run hit the sandbox's wall limit and was killed. Try a smaller load.",
}


def structured(tool: Callable[..., dict[str, Any]]) -> Callable[..., dict[str, Any]]:
    """No exception leaves a tool. An unexpected one is reported by type, not by its text."""

    @wraps(tool)
    def wrapper(*args: Any, **kwargs: Any) -> dict[str, Any]:
        try:
            return tool(*args, **kwargs)
        except FaultForgeError as error:
            return error.to_dict()
        except Exception as error:
            return FaultForgeError(
                "engine_failed", f"unexpected {type(error).__name__}", "This is a FaultForge bug; the run state is unchanged.",
            ).to_dict()

    return wrapper


# Reading a project


def _load_stack(path: Path) -> StackConfig:
    try:
        if path.is_symlink() or path.stat().st_size > MAX_STACK_BYTES:
            raise FaultForgeError("config_invalid", f"{STACK_NAME} must be a regular file under {MAX_STACK_BYTES} bytes")
        text = path.read_text(encoding="utf-8")
        config_module._reject_hostile_yaml(text)
        return StackConfig.model_validate(yaml.safe_load(text))
    except FaultForgeError:
        raise
    except (OSError, UnicodeDecodeError, yaml.YAMLError, ValidationError, ValueError, TypeError) as error:
        raise FaultForgeError("config_invalid", f"{STACK_NAME} is invalid: {store.clean(error, 600)}", f"Fix {STACK_NAME}.") from error


def stack_problems(stack: StackConfig) -> list[str]:
    """The MCP server runs a project's own stack.yaml, so it can't trust it the way the demos are."""
    problems = []
    if not POSTGRES_IMAGE.fullmatch(stack.postgres_image):
        problems.append("postgres_image must be a stock postgres:<tag> image")
    if not REDIS_IMAGE.fullmatch(stack.redis_image):
        problems.append("redis_image must be a stock redis:<tag> image")
    if stack.app_image is None:
        problems.append("app_image must name the prebuilt image to run")
    elif not APP_IMAGE.fullmatch(stack.app_image):
        problems.append("app_image must be a plain local image name such as myapp:buggy")
    uid, gid = (int(part) for part in stack.app_user.split(":"))
    if uid == 0 or gid == 0:
        problems.append("app_user must not be root")
    if any(not ENV_NAME.fullmatch(name) or "\0" in value for name, value in stack.env.items()):
        problems.append("env names must be plain variable names")
    if any(not WRITABLE_PATH.fullmatch(path) or ".." in path.split("/") for path in stack.app_writable):
        problems.append("app_writable entries must be plain absolute paths")
    try:
        safety.validate_path(stack.app_health_path)
    except FaultForgeError:
        problems.append("app_health_path must be a plain path such as /health")
    ports = (stack.app_port, stack.hooks_port)
    if not all(1024 <= port <= 65535 and port not in RESERVED_PORTS for port in ports) or len(set(ports)) != 2:
        problems.append("app_port and hooks_port must be two different ports from 1024 to 65535, not 5432 or 6379")
    if not 1 <= len(stack.state_sql) <= sandbox.MAX_STATE_FIELDS or not all(
        sandbox.STATE_FIELD_NAME.fullmatch(name) for name in stack.state_sql
    ):
        problems.append(f"state_sql needs 1 to {sandbox.MAX_STATE_FIELDS} plain lowercase field names")
    return problems


def _regular(path: Path) -> bool:
    try:
        return stat.S_ISREG(path.lstat().st_mode)
    except FileNotFoundError:
        return False


def python_support(root: Path) -> str | None:
    """The one package beside app.py, if any. More than one isn't supported yet."""
    if not _regular(root / APP_NAME):
        raise FaultForgeError(
            "unsupported_build",
            f"no {APP_NAME} at the project root",
            "A Python target is app.py (served as `uvicorn app:app`) with at most one package beside it. "
            "A stack target needs stack.yaml.",
        )
    packages = sorted(
        entry.name for entry in root.iterdir()
        if not entry.is_symlink() and entry.is_dir() and entry.name.isidentifier() and _regular(entry / "__init__.py")
    )
    if len(packages) > 1:
        raise FaultForgeError(
            "unsupported_build",
            f"more than one package beside {APP_NAME}: {', '.join(packages[:5])}",
            "M8 runs app.py with at most one support package.",
        )
    return packages[0] if packages else None


def _kind(project: Path) -> str:
    return "stack" if (project / STACK_NAME).exists() or (project / STACK_NAME).is_symlink() else "python"


def _busy() -> FaultForgeError:
    return FaultForgeError("busy", "another run is in progress", "Wait for it to finish, then call again.")


@contextlib.contextmanager
def run_lock(state: Path) -> Iterator[None]:
    """A non-blocking flock on <state>/run.lock, so a second server process sharing the state dir gets
    `busy` rather than two runs at once. The kernel drops it when the descriptor closes, so a killed
    server never leaves it held."""
    private_dir(state)
    descriptor = os.open(state / RUN_LOCK_NAME, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            raise _busy() from error
        yield
    finally:
        os.close(descriptor)


def run_lock_held(state: Path) -> bool:
    """True when another process (or this one) holds the run lock. A probe from a separate descriptor
    conflicts with our own lock too, so a busy server always reports busy."""
    try:
        descriptor = os.open(state / RUN_LOCK_NAME, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError:
        return False
    try:
        fcntl.flock(descriptor, fcntl.LOCK_SH | fcntl.LOCK_NB)
    except OSError:
        return True
    finally:
        os.close(descriptor)
    return False


def _image_present(image: str) -> bool | None:
    try:
        return sandbox._docker(STATUS_POLICY, ["image", "inspect", "--format", "{{.Id}}", "--", image]).returncode == 0
    except sandbox.SandboxError:
        return None


def _hooks_problems(config: ProjectConfig, stack: StackConfig | None, scenario_ids: list[str]) -> tuple[list[str], list[str]]:
    errors, warnings = [], []
    if config.target.origin != sandbox.SANDBOX_ORIGIN:
        errors.append(f"target.origin must be {sandbox.SANDBOX_ORIGIN}, where the sandbox serves the target")
    if stack is None:
        warnings.append("The hooks aren't contacted: the target only ever runs inside a sandbox, so they're checked at run time.")
        return errors, warnings
    if config.target.hooks_origin is None:
        errors.append("a stack target needs target.hooks_origin, served by the trusted hooks helper")
    fields = set(stack.state_sql)
    for scenario in config.scenarios:
        if scenario.id not in scenario_ids:
            continue
        for invariant in scenario.invariants:
            names = expressions.parse(invariant.check).variables
            missing = sorted(name for name in names if not name.startswith("client.") and name.split(".", 1)[1] not in fields)
            if missing:
                errors.append(f"{scenario.id}: invariant {store.clean(invariant.name, 80)!r} uses {', '.join(missing)}, not in state_sql")
    return errors, warnings


class Tools:
    """One run at a time per server process. A call that finds a run in progress gets `busy`."""

    def __init__(
        self,
        *,
        settings: Callable[[], McpSettings] = load_settings,
        run_sandboxed: Callable[..., SandboxResult] = sandbox.run_sandboxed,
        run_stack: Callable[..., SandboxResult] = stack_module.run_stack,
        image_present: Callable[[str], bool | None] = _image_present,
    ):
        self._settings = settings
        self._run_sandboxed = run_sandboxed
        self._run_stack = run_stack
        self._image_present = image_present
        self._lock = threading.Lock()

    # faultforge_status

    @structured
    def status(self) -> dict[str, Any]:
        settings = self._settings()
        try:
            containers: int | None = sandbox.count_labelled(STATUS_POLICY)
            docker = {"reachable": True, "sandbox_containers": containers}
        except sandbox.SandboxError as error:
            docker = {"reachable": False, "reason": error.reason}
        images = {
            image: self._image_present(image) if docker["reachable"] else None
            for image in (sandbox.DEFAULT_POLICY.target_image, sandbox.DEFAULT_POLICY.runner_image, HOOKS_IMAGE)
        }
        try:
            k6_runner.find_k6()
            k6 = "found on the host; the runner image carries its own"
        except k6_runner.K6Missing:
            k6 = "not on the host; the runner image carries its own"
        local_ready = bool(docker["reachable"]) and all(images[image] for image in list(images)[:2])
        return {
            "status": "ok",
            "docker": docker,
            "images": images,
            "k6": k6,
            "runtimes": {
                "local": {"available": local_ready, "reason": None if local_ready else "Docker or the sandbox images are missing"},
                "remote": {"available": False, "reason": REMOTE_UNAVAILABLE},
            },
            "fix_modes": {
                "client": {"available": False, "reason": "arrives in M9"},
                "vultr": {"available": False, "reason": "arrives in M10"},
            },
            "busy": self._lock.locked() or run_lock_held(settings.state_dir),
            "allowed_roots": [str(root) for root in settings.allowed_roots],
            "config_path": str(settings.config_path),
            "config_problems": list(settings.problems),
            "state_dir": str(settings.state_dir),
            "credit": None,
            "credit_reason": "credit check arrives in M10",
        }

    # faultforge_list_scenarios

    @structured
    def list_scenarios(self, project_dir: str, offset: int = 0) -> dict[str, Any]:
        """Reads the working tree's config. It sends nothing to any target. One page of scenarios at a
        time, so the result stays within about 60 lines."""
        if type(offset) is not int or not 0 <= offset <= MAX_SCENARIOS_TOTAL:
            raise FaultForgeError("invalid_argument", "offset must be a non-negative integer")
        settings = self._settings()
        project = resolve_project(project_dir, settings)
        config = config_module.load_config(project)
        result: dict[str, Any] = {"status": "ok", "project_dir": str(project), "kind": _kind(project)}
        if result["kind"] == "stack":
            stack = _load_stack(project / STACK_NAME)
            allowed = stack.app_image is not None and stack.app_image in settings.allowed_images
            result["stack"] = {
                "app_image": stack.app_image,
                "app_image_allowed": allowed,
                "app_image_present": (
                    self._image_present(stack.app_image) if allowed and APP_IMAGE.fullmatch(stack.app_image or "") else None
                ),
                "sources": "image",
                "problems": stack_problems(stack) + ([] if allowed else [f"app_image isn't in allowed_images in {settings.config_path}"]),
            }
        else:
            result["sources"] = "worktree | commit:<sha>"
        page = config.scenarios[offset: offset + MAX_SCENARIOS_LISTED]
        result["scenarios"] = [
            {
                "id": scenario.id,
                "description": store.clean(scenario.description, 120),
                "flags": ", ".join(
                    name for name, flag in (("mutates_data", scenario.mutates_data), ("user_confirmed", scenario.user_confirmed)) if flag
                ) or "none",
                "load": f"{scenario.load.vus} vus, {scenario.load.iterations} iterations, {scenario.load.max_duration_s} s",
                "invariants": ", ".join(store.clean(invariant.name, 60) for invariant in scenario.invariants),
            }
            for scenario in page
        ]
        result["scenarios_total"] = len(config.scenarios)
        remaining = len(config.scenarios) - offset - len(page)
        if remaining > 0:
            result["more"] = remaining
            result["next_offset"] = offset + len(page)
        result["limits"] = f"at most {LIMITS.max_vus} vus, {LIMITS.max_iterations_per_trial} iterations per trial, {LIMITS.max_trial_duration_s} s per trial"
        return result

    # faultforge_validate_scenario

    @structured
    def validate_scenario(self, project_dir: str, scenario_id: str | None = None) -> dict[str, Any]:
        """Schema, safety, expressions and, for a stack, that state_sql covers every variable.
        It sends no load and never calls reset."""
        settings = self._settings()
        project = resolve_project(project_dir, settings)
        errors: list[str] = []
        warnings: list[str] = []
        result: dict[str, Any] = {"status": "ok", "project_dir": str(project), "kind": _kind(project)}
        try:
            config = config_module.load_config(project)
        except FaultForgeError as error:
            return {**result, "valid": False, "errors": [store.clean(error.message, 600)], "warnings": [], "user_confirmed": None}
        ids = [scenario.id for scenario in config.scenarios]
        if scenario_id is not None:
            try:
                config_module.find_scenario(config, scenario_id)
                ids = [scenario_id]
            except FaultForgeError as error:
                errors.append(store.clean(f"{error.message}. {error.fix_hint}", 600))
                ids = []
        stack = None
        if result["kind"] == "stack":
            try:
                stack = _load_stack(project / STACK_NAME)
                errors += stack_problems(stack)
                if not errors:
                    require_image_allowed(stack.app_image, settings)
            except FaultForgeError as error:
                errors.append(f"{error.message}. {error.fix_hint}" if error.fix_hint else error.message)
        else:
            try:
                python_support(project)
            except FaultForgeError as error:
                errors.append(f"{error.message}. {error.fix_hint}")
            if snapshot_module.git(project, "rev-parse", "--is-inside-work-tree").returncode != 0:
                warnings.append("Not a git working tree, so faultforge_run_scenario can't snapshot it.")
        if stack is not None or result["kind"] == "python":
            hook_errors, hook_warnings = _hooks_problems(config, stack, ids)
            errors += hook_errors
            warnings += hook_warnings
        chosen = [scenario for scenario in config.scenarios if scenario.id in ids]
        warnings += [f"{scenario.id} isn't marked user_confirmed" for scenario in chosen if not scenario.user_confirmed]
        return {
            **result,
            "valid": not errors,
            "scenarios_checked": ids,
            "errors": errors[:20],
            "warnings": warnings[:20],
            "user_confirmed": all(scenario.user_confirmed for scenario in chosen) if chosen else None,
        }

    # faultforge_run_scenario

    @structured
    def run_scenario(
        self, project_dir: str, scenario_id: str, source: str, runtime: str = "auto", compare_to: str | None = None,
    ) -> dict[str, Any]:
        if not isinstance(scenario_id, str) or not SCENARIO_ID.fullmatch(scenario_id):
            raise FaultForgeError("invalid_argument", "scenario_id must be a scenario id such as last-ticket-rush")
        kind, sha = snapshot_module.parse_source(source)
        if runtime == "remote":
            raise FaultForgeError("runtime_unavailable", f"the remote runtime is {REMOTE_UNAVAILABLE}", "Use runtime \"local\" or \"auto\".")
        if runtime not in ("auto", "local"):
            raise FaultForgeError("invalid_argument", "runtime must be auto, local or remote")
        if compare_to is not None:
            if not isinstance(compare_to, str):
                raise FaultForgeError("invalid_argument", "compare_to must be a run_id")
            safety.validate_run_id(compare_to)
        settings = self._settings()
        project = resolve_project(project_dir, settings)
        if not self._lock.acquire(blocking=False):
            raise _busy()
        try:
            # The in-process lock first, then the cross-process one; both are released on every path.
            with run_lock(settings.state_dir):
                return self._run(settings, project, scenario_id, kind, sha, compare_to)
        finally:
            self._lock.release()

    def _run(
        self, settings: McpSettings, project: Path, scenario_id: str, kind: str, sha: str | None, compare_to: str | None,
    ) -> dict[str, Any]:
        state = private_dir(settings.state_dir)
        store.sweep_stale(state, time.time())
        baseline = store.read_meta(state, compare_to) if compare_to is not None else None
        if baseline is not None and (baseline.project_dir != str(project) or baseline.scenario_id != scenario_id):
            raise FaultForgeError(
                "not_comparable", f"{compare_to} is a run of {baseline.scenario_id} in {baseline.project_dir}",
                "compare_to must be an earlier run of the same scenario in the same project.",
            )
        target_kind = _kind(project)
        notes: list[str] = []
        if target_kind == "stack":
            if kind != "image":
                raise FaultForgeError(
                    "unsupported_build", "building a stack target from source isn't supported yet",
                    "Building from source arrives in M11. Use source \"image\" to run the prebuilt image that stack.yaml names.",
                )
            snap = snapshot_module.copy_files(project, state, [config_module.CONFIG_NAME, STACK_NAME], "image")
        elif kind == "image":
            raise FaultForgeError("invalid_argument", "source \"image\" is only for stack targets", "Use \"worktree\" or \"commit:<sha>\".")
        elif kind == "commit":
            snap = snapshot_module.snapshot_commit(project, state, sha or "", settings.allowed_roots)
        else:
            snap = snapshot_module.snapshot_worktree(project, state, settings.allowed_roots)
        work: Path | None = None
        try:
            work = private_dir(state / "work") / secrets.token_hex(8)
            config_path = snap.root / config_module.CONFIG_NAME
            config = config_module.load_config(snap.root)
            config_module.find_scenario(config, scenario_id)
            if target_kind == "stack":
                stack = _load_stack(snap.root / STACK_NAME)
                problems = stack_problems(stack)
                if problems:
                    raise FaultForgeError("config_invalid", f"{STACK_NAME}: {'; '.join(problems)}", f"Fix {STACK_NAME}.")
                require_image_allowed(stack.app_image, settings)
                notes.append(
                    f"Ran the prebuilt image {stack.app_image}. The source was not rebuilt; building from source arrives in M11. "
                    f"{config_module.CONFIG_NAME} and {STACK_NAME} came from the working tree."
                )
                run = lambda: self._run_stack(StackSpec(stack.app_image, stack, config_path), work, scenario_id, RunOptions(compare_to=compare_to))  # noqa: E731
                source = {**snap.describe(), "app_image": stack.app_image, "rebuilt": False}
            else:
                if _regular(snap.root / STACK_NAME):
                    raise FaultForgeError("unsupported_build", f"the snapshot has a {STACK_NAME} the working tree doesn't", "Run the stack with source \"image\".")
                support = python_support(snap.root)
                app_source = (snap.root / APP_NAME).read_bytes()
                spec = TargetSpec(app_source=app_source, config_path=config_path, support_dir=snap.root / support if support else None)
                run = lambda: self._run_sandboxed(spec, work, scenario_id, RunOptions(compare_to=compare_to))  # noqa: E731
                source = snap.describe()
                if snap.dropped_secrets:
                    notes.append(f"Left {len(snap.dropped_secrets)} secret-looking files out of the snapshot.")
            work.mkdir(mode=0o700)
            if compare_to is not None:
                store.stage_baseline(state, work, compare_to)
            result = run()
            if result.status != "completed" or result.report is None:
                return _failure(result)
            facts = [item for item in (result.containment.target, result.containment.runner) if item is not None]
            verification = result.containment.state_verification
            meta = store.RunMeta(
                run_id=result.report.run_id, project_dir=str(project), scenario_id=scenario_id, kind=target_kind,
                source=source, compare_to=compare_to, deployed_code_sha256=result.deployed_code_sha256,
                containers_left=result.containment.containers_left,
                limits_applied=all(item.limits_applied for item in [*facts, *result.containment.services]) if facts else None,
                state_verification=verification.status if verification else "not requested",
                notes=notes,
            )
            store.store_run(state, work, result.report, meta, result)
            return {**store.summary(state, result.report.run_id), "mutates_data": "only the sandbox's own throwaway data"}
        finally:
            snap.remove()
            if work is not None:
                store.remove_tree(work)

    # faultforge_get_run

    @structured
    def get_run(
        self, run_id: str, section: str = "summary", offset: int = 0, limit: int = store.DEFAULT_PAGE,
        trial: int | None = None, outcome: str | None = None,
    ) -> dict[str, Any]:
        state = self._settings().state_dir
        store.run_path(state, run_id)
        if section not in store.SECTIONS:
            raise FaultForgeError("invalid_argument", f"section must be one of {', '.join(store.SECTIONS)}")
        offset, limit = store.page_bounds(offset, limit)
        if trial is not None and (type(trial) is not int or trial < 1):
            raise FaultForgeError("invalid_argument", "trial must be a positive integer")
        if section == "summary":
            return store.summary(state, run_id)
        if section == "violations":
            return store.violations(state, run_id, offset, limit)
        if section == "hypotheses":
            return store.hypotheses(state, run_id, offset, limit)
        if section == "performance":
            return store.performance(state, run_id)
        return store.evidence(state, run_id, offset, limit, trial, outcome)


def _failure(result: SandboxResult) -> dict[str, Any]:
    reason = result.failure_reason or "internal_error"
    error_class = {"not_comparable": "not_comparable", "invalid_spec": "config_invalid"}.get(reason, "sandbox_failed")
    detail = store.clean(result.failure_detail or "", 300)
    error = FaultForgeError(
        error_class,  # type: ignore[arg-type]
        f"the sandboxed run failed: {reason}" + (f" (detail, may quote the target: {detail})" if detail else ""),
        FAILURE_HINTS.get(reason, "Call faultforge_status to check Docker and the images, then try again."),
    ).to_dict()
    error["containers_left"] = result.containment.containers_left
    return error

