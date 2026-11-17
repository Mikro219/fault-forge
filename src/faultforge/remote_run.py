"""The runner VM's one entry point for the remote runtime (docs/PLAN.md V4.6, M12).

The MCP server on the owner's machine calls it over SSH, always the same way:

    ssh runner@<vm> /home/runner/faultforge/app/<commit>/.venv/bin/python -I -m faultforge.remote_run

No argument follows, so no project text is ever in a command line or near a shell. Everything else
travels on the pipes:

- stdin: one JSON line, the request, then a tar stream of the inputs until EOF;
- stdout: one JSON line, the answer, then exactly `answer["tar_bytes"]` bytes of a tar of the run
  directory, if there is one.

The VM runs the same code as the owner's machine: sandbox.run_sandboxed, stack.run_stack and
node_build.build_stack_image, with the same images, which the deploy step moved there by `docker
save | docker load`. Every request carries the code hash and image IDs the server expects, and a VM
that doesn't match refuses before it touches anything.

Paths here are fixed constants. Nothing in a request names a path: each request gets a new random
directory under snapshots/, and it is removed before the answer is written, on every path.
"""

import dataclasses
import hashlib
import io
import json
import os
import re
import secrets
import shutil
import signal
import stat
import sys
import tarfile
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, BinaryIO

BASE = Path("/home/runner/faultforge")
SNAPSHOTS = "snapshots"
STATE = "state"
OP_DIR = re.compile(r"[a-f0-9]{16}")
STALE_SNAPSHOT_S = 2 * 3600
MAX_REQUEST_BYTES = 1024 * 1024
MAX_INPUT_BYTES = 128 * 1024 * 1024
MAX_OUTPUT_BYTES = 96 * 1024 * 1024
MAX_MEMBERS = 20_000
IMAGE_NAME = re.compile(r"[a-z0-9][a-z0-9._/-]{0,127}:[A-Za-z0-9._-]{1,128}")
IMAGE_ID = re.compile(r"sha256:[a-f0-9]{64}")
SHA256 = re.compile(r"[a-f0-9]{64}")
SUPPORT_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}")
OPS = ("ping", "run_python", "run_stack", "build_stack", "remove_images")
OPTION_FIELDS = {"trials": int, "serial_control": bool, "vus": int, "iterations": int, "max_duration_s": int, "compare_to": str}


class Refused(Exception):
    """A request the VM won't act on. The message is fixed text: it never quotes the request."""


class Deadline(BaseException):
    """Not an Exception, so the run code's own `except Exception` can't swallow it: it unwinds to
    serve() through the run code's finally blocks. If it lands during a container removal, that
    removal stops, and the containers are left to the reaper by their expiry labels."""


# Shared with the host: the code hash, and the strict tar format both sides use.


def package_sha256(root: Path | None = None) -> str:
    """Every regular file of the faultforge package except bytecode, by sorted relative path, with
    lengths so the boundaries are unambiguous. The host and the VM compute it over their own copies,
    so a VM running other code is caught whatever its deploy record says."""
    if root is None:
        root = Path(__file__).resolve().parent
    digest = hashlib.sha256()
    entries = []
    for directory, subdirs, files in os.walk(root, followlinks=False):
        subdirs[:] = sorted(name for name in subdirs if name != "__pycache__")
        for name in files:
            path = Path(directory) / name
            if name.endswith((".pyc", ".pyo")) or not stat.S_ISREG(path.lstat().st_mode):
                continue
            entries.append((path.relative_to(root).as_posix(), path))
    for relative, path in sorted(entries):
        name = relative.encode("utf-8", "surrogateescape")
        content = path.read_bytes()
        digest.update(len(name).to_bytes(8, "big") + name + len(content).to_bytes(8, "big") + content)
    return digest.hexdigest()


def _member_path(name: str) -> Path:
    path = Path(name)
    if not name or path.is_absolute() or ".." in path.parts or any(ord(char) < 32 for char in name) or "\\" in name:
        raise Refused("a tar member's path escapes its directory")
    return path


def pack(entries: Mapping[str, Path | bytes], max_bytes: int = MAX_INPUT_BYTES) -> bytes:
    """A tar of plain files and directories. Each key is a top-level name; a Path value is a file or
    a directory tree, walked without following links (a link or special file is refused), and a
    bytes value is one file's content."""
    buffer = io.BytesIO()
    total = 0
    with tarfile.open(fileobj=buffer, mode="w", format=tarfile.PAX_FORMAT) as archive:

        def add_bytes(name: str, data: bytes) -> None:
            nonlocal total
            total += len(data)
            if total > max_bytes:
                raise Refused("the archive is too large")
            info = tarfile.TarInfo(name)
            info.size, info.mode, info.mtime = len(data), 0o644, 0
            archive.addfile(info, io.BytesIO(data))

        def add_dir(name: str) -> None:
            info = tarfile.TarInfo(name)
            info.type, info.mode, info.mtime = tarfile.DIRTYPE, 0o755, 0
            archive.addfile(info)

        for top, value in entries.items():
            _member_path(top)
            if isinstance(value, bytes):
                add_bytes(top, value)
                continue
            info = value.lstat()
            if stat.S_ISREG(info.st_mode):
                add_bytes(top, value.read_bytes())
                continue
            if not stat.S_ISDIR(info.st_mode):
                raise Refused("only plain files and directories are sent")
            add_dir(top)
            for directory, subdirs, files in os.walk(value, followlinks=False):
                subdirs.sort()
                here = Path(directory)
                for name in subdirs:
                    if not stat.S_ISDIR((here / name).lstat().st_mode):
                        raise Refused("only plain files and directories are sent")
                    add_dir(f"{top}/{(here / name).relative_to(value).as_posix()}")
                for name in sorted(files):
                    path = here / name
                    if not stat.S_ISREG(path.lstat().st_mode):
                        raise Refused("only plain files and directories are sent")
                    add_bytes(f"{top}/{path.relative_to(value).as_posix()}", path.read_bytes())
    return buffer.getvalue()


RUN_DIR_FILES = re.compile(r"(report\.json|report\.md|requests\.jsonl|trials/[0-9]{1,3}/(k6-summary\.json|k6\.log|state-before\.json|state-after\.json))")
RUN_DIR_DIRS = re.compile(r"(trials|trials/[0-9]{1,3})")


def run_dir_member(run_id: str) -> Callable[[Path, bool], bool]:
    """What may come back from a run: that run's directory, and only the names a run writes there."""
    prefix = ("work", ".faultforge", "runs", run_id)

    def allowed(relative: Path, is_dir: bool) -> bool:
        parts = relative.parts
        if len(parts) <= len(prefix):
            return is_dir and parts == prefix[: len(parts)]
        if parts[: len(prefix)] != prefix:
            return False
        rest = "/".join(parts[len(prefix):])
        return bool(RUN_DIR_DIRS.fullmatch(rest)) if is_dir else bool(RUN_DIR_FILES.fullmatch(rest))

    return allowed


def unpack(
    stream: BinaryIO, destination: Path, max_bytes: int, tops: tuple[str, ...], max_members: int = MAX_MEMBERS,
    allow: Callable[[Path, bool], bool] | None = None,
) -> int:
    """Rebuilds a tar stream as plain files and directories under destination, which must exist.
    Anything else (a link of either kind, a device, a FIFO), a path that leaves the directory, a
    top-level name the caller didn't ask for, too many members or too many bytes is refused, each
    checked from the header before anything is written. Returns the bytes written."""
    total = 0
    seen = 0
    with tarfile.open(fileobj=stream, mode="r|") as archive:
        for member in archive:
            seen += 1
            if seen > max_members:
                raise Refused("the archive has too many members")
            relative = _member_path(member.name)
            if not relative.parts:
                # "." is the archive's own top; a directory entry for it says nothing.
                if member.isdir():
                    continue
                raise Refused("the archive holds something other than plain files and directories")
            if relative.parts[0] not in tops:
                raise Refused("the archive holds something that wasn't asked for")
            if allow is not None and not allow(relative, member.isdir()):
                raise Refused("the archive holds a name a run doesn't write")
            target = destination / relative
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True, mode=0o700)
            elif member.isreg():
                total += member.size
                if total > max_bytes:
                    raise Refused("the archive is too large")
                target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                source = archive.extractfile(member)
                if source is None:
                    raise Refused("a tar member couldn't be read")
                # O_EXCL and O_NOFOLLOW: nothing written earlier can redirect this write.
                descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
                with os.fdopen(descriptor, "wb") as out:
                    shutil.copyfileobj(source, out)
            else:
                raise Refused("the archive holds something other than plain files and directories")
    return total


# The VM side


def snapshots_dir(base: Path) -> Path:
    path = base / SNAPSHOTS
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.is_symlink() or not path.is_dir():
        raise Refused("the snapshots directory isn't a directory")
    return path


def reap(base: Path, now: float | None = None, max_age_s: float = STALE_SNAPSHOT_S) -> int:
    """Removes request directories a killed request left, older than two hours. Only names this module
    makes are looked at."""
    now = time.time() if now is None else now
    removed = 0
    for entry in snapshots_dir(base).iterdir():
        try:
            if OP_DIR.fullmatch(entry.name) and not entry.is_symlink() and now - entry.lstat().st_mtime >= max_age_s:
                shutil.rmtree(entry, ignore_errors=True)
                removed += 1
        except OSError:
            continue
    return removed


def _image_ids(names: list[str]) -> dict[str, str | None]:
    from faultforge import node_build

    return {name: node_build._image_id(node_build.POLICY, name) for name in names}


def _sandbox_count() -> int | None:
    from faultforge import sandbox

    try:
        return sandbox.count_labelled(sandbox.DEFAULT_POLICY)
    except sandbox.SandboxError:
        return None


@dataclasses.dataclass
class Runners:
    """What the VM calls. Tests swap these for fakes; the real ones need Docker and the images."""

    run_sandboxed: Callable[..., Any] | None = None
    run_stack: Callable[..., Any] | None = None
    build_stack: Callable[..., Any] | None = None
    remove_images: Callable[[list[str]], None] | None = None
    image_ids: Callable[[list[str]], dict[str, str | None]] = _image_ids
    sandbox_count: Callable[[], int | None] = _sandbox_count
    reap_containers: Callable[[], None] | None = None
    candidate_images: Callable[[list[str]], list[str]] | None = None
    code_sha256: Callable[[], str] = package_sha256


def real_runners() -> Runners:
    from faultforge import node_build, sandbox, stack

    def reap_containers() -> None:
        # Containers and networks only. Built images are swept by the build step itself, as locally,
        # so a long call never loses the image it built earlier.
        try:
            sandbox.reap_stale(sandbox.DEFAULT_POLICY)
        except sandbox.SandboxError:
            pass

    def candidate_images(ids: list[str]) -> list[str]:
        """Only images a build made here, by their own label: nothing else is ever removed."""
        if not ids:
            return []
        described = sandbox._docker(node_build.POLICY, ["image", "inspect", "--format", "{{.Id}} {{json .Config.Labels}}", *ids])
        found = []
        for line in described.stdout.decode("utf-8", "replace").splitlines():
            identifier, _, raw = line.partition(" ")
            try:
                labels = json.loads(raw)
            except ValueError:
                continue
            if identifier in ids and isinstance(labels, dict) and labels.get(node_build.CANDIDATE_LABEL) == "1":
                found.append(identifier)
        return found

    return Runners(
        run_sandboxed=sandbox.run_sandboxed, run_stack=stack.run_stack, build_stack=node_build.build_stack_image,
        remove_images=node_build.remove_images, reap_containers=reap_containers, candidate_images=candidate_images,
    )


def _options(raw: object):
    from faultforge import safety
    from faultforge.lifecycle import RunOptions

    if not isinstance(raw, dict) or set(raw) - set(OPTION_FIELDS):
        raise Refused("the run options aren't valid")
    for name, value in raw.items():
        if value is not None and type(value) is not OPTION_FIELDS[name]:
            raise Refused("the run options aren't valid")
    if raw.get("compare_to") is not None:
        safety.validate_run_id(raw["compare_to"])
    return RunOptions(**raw)


def _scenario(raw: object) -> str:
    if not isinstance(raw, str) or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", raw):
        raise Refused("the scenario id isn't valid")
    return raw


def _check_deploy(request: dict, runners: Runners) -> None:
    expect = request.get("expect")
    if not isinstance(expect, dict) or not isinstance(expect.get("images"), dict) or not expect["images"]:
        raise Refused("the request doesn't say which deploy it expects")
    names = list(expect["images"])
    if len(names) > 32 or not all(isinstance(name, str) and IMAGE_NAME.fullmatch(name) for name in names):
        raise Refused("the expected image names aren't valid")
    if expect.get("code_sha256") != runners.code_sha256():
        raise Refused("deploy_mismatch: this VM runs other FaultForge code")
    if runners.image_ids(names) != expect["images"]:
        raise Refused("deploy_mismatch: this VM's images aren't the expected ones")


def _receive(stdin: BinaryIO, root: Path, tops: tuple[str, ...]) -> None:
    unpack(stdin, root, MAX_INPUT_BYTES, tops)


def _ping(request: dict, runners: Runners, base: Path) -> dict:
    names = request.get("images", [])
    if not isinstance(names, list) or len(names) > 32 or not all(isinstance(name, str) and IMAGE_NAME.fullmatch(name) for name in names):
        raise Refused("the image names aren't valid")
    entries = [entry for entry in snapshots_dir(base).iterdir() if OP_DIR.fullmatch(entry.name)]
    return {
        "ok": True, "code_sha256": runners.code_sha256(), "images": runners.image_ids(names),
        "snapshots": len(entries), "sandbox_containers": runners.sandbox_count(),
    }


def _run_python(request: dict, runners: Runners, root: Path, stdin: BinaryIO) -> tuple[dict, Path | None]:
    from faultforge.sandbox import TargetSpec

    support = request.get("support")
    if support is not None and (not isinstance(support, str) or not SUPPORT_NAME.fullmatch(support)):
        raise Refused("the support package name isn't valid")
    verifier = request.get("state_verifier")
    if verifier is not None and (
        not isinstance(verifier, dict) or not all(isinstance(key, str) and isinstance(value, str) for key, value in verifier.items())
    ):
        raise Refused("the state verifier isn't valid")
    scenario_id, options = _scenario(request.get("scenario_id")), _options(request.get("options"))
    _receive(stdin, root, ("app.py", "config", "support", "work"))
    app = root / "app.py"
    config = root / "config" / "faultforge.yaml"
    if not app.is_file() or not config.is_file():
        raise Refused("the inputs are incomplete")
    support_dir = root / "support" / support if support else None
    if support_dir is not None and not support_dir.is_dir():
        raise Refused("the inputs are incomplete")
    work = root / "work"
    work.mkdir(mode=0o700, exist_ok=True)
    spec = TargetSpec(app_source=app.read_bytes(), config_path=config, support_dir=support_dir, allow_hostile=False, state_verifier=verifier)
    result = runners.run_sandboxed(spec, work, scenario_id, options)
    return {"ok": True, "result": result.model_dump(mode="json")}, _run_dir(work, result)


def _run_stack(request: dict, runners: Runners, root: Path, stdin: BinaryIO) -> tuple[dict, Path | None]:
    from faultforge.stack import StackConfig, StackSpec

    image = request.get("app_image")
    if not isinstance(image, str) or not IMAGE_ID.fullmatch(image):
        raise Refused("a remote stack runs an image built here, by its ID")
    code = request.get("code_sha256")
    if code is not None and (not isinstance(code, str) or not SHA256.fullmatch(code)):
        raise Refused("the code hash isn't valid")
    stack = StackConfig.model_validate(request.get("stack"))
    scenario_id, options = _scenario(request.get("scenario_id")), _options(request.get("options"))
    _receive(stdin, root, ("config", "work"))
    config = root / "config" / "faultforge.yaml"
    if not config.is_file():
        raise Refused("the inputs are incomplete")
    work = root / "work"
    work.mkdir(mode=0o700, exist_ok=True)
    result = runners.run_stack(StackSpec(image, stack, config, code_sha256=code), work, scenario_id, options)
    return {"ok": True, "result": result.model_dump(mode="json")}, _run_dir(work, result)


def _run_dir(work: Path, result) -> Path | None:
    """Only the run this request made goes back: the host has everything else already."""
    if result.report is None:
        return None
    from faultforge import safety

    safety.validate_run_id(result.report.run_id)
    path = work / ".faultforge" / "runs" / result.report.run_id
    return path if path.is_dir() and not path.is_symlink() else None


def _build_stack(request: dict, runners: Runners, root: Path, stdin: BinaryIO, base: Path) -> tuple[dict, Path | None]:
    from faultforge.node_build import BuildConfig

    build = BuildConfig.model_validate(request.get("build"))
    allow_fetch = request.get("allow_fetch")
    if type(allow_fetch) is not bool:
        raise Refused("allow_fetch must be true or false")
    _receive(stdin, root, ("source",))
    source = root / "source"
    source.mkdir(mode=0o700, exist_ok=True)
    state = base / STATE
    state.mkdir(mode=0o700, parents=True, exist_ok=True)
    built = runners.build_stack(source, build, state, allow_fetch=allow_fetch)
    return {"ok": True, "build": dataclasses.asdict(built)}, None


def _remove_images(request: dict, runners: Runners) -> dict:
    images = request.get("images")
    if not isinstance(images, list) or len(images) > 16 or not all(isinstance(item, str) and IMAGE_ID.fullmatch(item) for item in images):
        raise Refused("only built images, by ID, are removed")
    chosen = runners.candidate_images(images) if runners.candidate_images else []
    if chosen:
        runners.remove_images(chosen)
    return {"ok": True, "removed": len(chosen)}


def _alarm(_signum, _frame) -> None:
    raise Deadline()


# How long each request may take on the VM, from its start. A run's containers carry an expiry label
# of their own start plus the wall limit and the margin, so by this deadline the reaper may take them.
def op_deadline_s(op: str) -> float:
    from faultforge import node_build, sandbox, stack

    if op == "run_python":
        return sandbox.DEFAULT_POLICY.wall_limit_s + sandbox.DEFAULT_POLICY.expiry_margin_s
    if op == "run_stack":
        return stack.STACK_POLICY.wall_limit_s + stack.STACK_POLICY.expiry_margin_s
    if op == "build_stack":
        fix = node_build._stack_fix()
        return (
            node_build.FETCH_TIMEOUT_S + node_build.LINK_TIMEOUT_S + fix.BUILD_TIMEOUT_S + 3 * fix.COPY_TIMEOUT_S
            + node_build.IMAGE_TIMEOUT_S + node_build.POLICY.expiry_margin_s
        )
    return 60.0


def serve(stdin: BinaryIO, stdout: BinaryIO, *, base: Path = BASE, runners: Runners | None = None, alarm: bool = False) -> int:
    """Answers one request. Every failure is an answer with a fixed reason, never a traceback."""
    root: Path | None = None
    output = b""
    answer: dict[str, Any]
    try:
        line = stdin.readline(MAX_REQUEST_BYTES + 1)
        if len(line) > MAX_REQUEST_BYTES or not line.endswith(b"\n"):
            raise Refused("the request line is too long or unterminated")
        request = json.loads(line)
        if not isinstance(request, dict) or request.get("op") not in OPS:
            raise Refused("unknown request")
        op = request["op"]
        if alarm:
            signal.signal(signal.SIGALRM, _alarm)
            signal.alarm(int(op_deadline_s(op)) + 1)
        runners = runners or real_runners()
        if op == "ping":
            # Request directories a killed request left are reaped here too, so a VM that only ever
            # answers pings (faultforge_status) still cleans up.
            reaped_on_ping = reap(base)
            answer = _ping(request, runners, base)
            answer["reaped_snapshots"] = reaped_on_ping
        else:
            _check_deploy(request, runners)
            # The reaper, at the start of every request that acts: old request directories, and
            # FaultForge's own labelled containers and built images past their expiry. Never a prune.
            reaped = reap(base)
            if runners.reap_containers is not None:
                runners.reap_containers()
            if op == "remove_images":
                answer = _remove_images(request, runners)
            else:
                root = snapshots_dir(base) / secrets.token_hex(8)
                root.mkdir(mode=0o700)
                if op == "run_python":
                    answer, work = _run_python(request, runners, root, stdin)
                elif op == "run_stack":
                    answer, work = _run_stack(request, runners, root, stdin)
                else:
                    answer, work = _build_stack(request, runners, root, stdin, base)
                if work is not None:
                    output = pack({f"work/.faultforge/runs/{work.name}": work}, MAX_OUTPUT_BYTES)
            answer.setdefault("reaped_snapshots", reaped)
    except Deadline:
        answer, output = {"ok": False, "error": "the request ran past its deadline on the VM"}, b""
    except Refused as error:
        answer, output = {"ok": False, "error": str(error)}, b""
    except (ValueError, tarfile.TarError, OSError, EOFError) as error:
        answer, output = {"ok": False, "error": f"the request failed on the VM ({type(error).__name__})"}, b""
    except Exception as error:  # noqa: BLE001 - reported by type, never by its text
        answer, output = {"ok": False, "error": f"unexpected {type(error).__name__} on the VM"}, b""
    finally:
        if alarm:
            signal.alarm(0)
        if root is not None:
            shutil.rmtree(root, ignore_errors=True)
    if root is not None:
        answer["snapshot_removed"] = not root.exists()
    answer["tar_bytes"] = len(output)
    stdout.write(json.dumps(answer, separators=(",", ":")).encode("utf-8") + b"\n")
    stdout.write(output)
    stdout.flush()
    return 0 if answer.get("ok") else 1


def main() -> int:
    return serve(sys.stdin.buffer, sys.stdout.buffer, alarm=True)


if __name__ == "__main__":
    sys.exit(main())
