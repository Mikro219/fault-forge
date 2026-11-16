"""Copies a project's source into a fresh private directory, so what runs is what was snapshotted
(docs/PLAN.md V4.5).

- `worktree` copies tracked files and untracked files that aren't ignored, from git's own list.
- `commit:<sha>` reads the commit's blobs with `git ls-tree` and `git cat-file --batch`. Not
  `git archive`: that runs the repo's filter drivers (`.gitattributes` plus smudge commands from git
  config) on the host, outside any sandbox, and an LFS smudge can reach the network.

Either way only regular files arrive. Symlinks, special files, anything under `.git`, any path that
resolves outside the project and anything named like a secret are left out before a file is written,
so nothing that reads the snapshot later can see them. Git runs with an argument list, a minimal
environment, no system or global config, no optional locks, no fsmonitor, no lazy fetch and no
transport protocol at all, so a snapshot never writes to the repo and runs no command the repo
configures. Every git call has a wall-clock deadline.

The project's git metadata is untrusted too. Before anything is read from the object store,
`require_repo_inside` checks that the repository git resolves for the project is the project's own:
its top level contains the project, and its git dir and common dir resolve inside that top level or
inside an allowed root. A `.git` file whose `gitdir:` points elsewhere, an `objects/info/alternates`
file, or a promisor (partial) clone is refused, so the allowlist bounds what git can read.
"""

import fnmatch
import os
import re
import secrets
import shutil
import stat
import subprocess
import tempfile
import threading
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import IO

from faultforge.errors import FaultForgeError
from faultforge.mcp_server.settings import private_dir

SECRET_PATTERNS = (
    ".env*", "*.pem", "*.key", "*.p12", "*.pfx", "id_rsa*", "id_ed25519*", "id_ecdsa*", ".npmrc", ".netrc", ".pypirc",
)
MAX_SNAPSHOT_BYTES = 50 * 1024 * 1024
MAX_SNAPSHOT_FILES = 5000
MAX_LISTING_BYTES = 16 * 1024 * 1024
MAX_PATH_CHARS = 1024
GIT_TIMEOUT_S = 30.0
COMMIT_PATTERN = re.compile(r"[0-9a-f]{7,64}")
FULL_COMMIT_PATTERN = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")
CHUNK = 64 * 1024
UNPRINTABLE = re.compile(r"[^\x20-\x7e]")
# protocol.allow=never: should anything still try to fetch (a promisor repo is refused before that), no
# transport runs, so the repo's core.sshCommand or a remote helper can't execute on the host.
GIT_ARGS = ("-c", "core.fsmonitor=false", "-c", "core.untrackedCache=false", "-c", "protocol.allow=never")
PROMISOR_CONFIG = r"^(extensions\.partialclone|remote\..*\.promisor)$"


@dataclass
class Snapshot:
    root: Path
    source: str
    commit: str | None = None
    files: int = 0
    bytes: int = 0
    dropped_secrets: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)

    def remove(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    def describe(self) -> dict[str, object]:
        return {
            "source": self.source,
            "commit": self.commit,
            "files": self.files,
            "bytes": self.bytes,
            "dropped_secrets": len(self.dropped_secrets),
            "skipped_links_or_special": len(self.skipped),
        }


def _clean(text: str, limit: int = 300) -> str:
    return UNPRINTABLE.sub("?", text)[:limit]


def parse_source(source: object) -> tuple[str, str | None]:
    """Returns ("worktree", None), ("image", None) or ("commit", sha)."""
    if source in ("worktree", "image"):
        return str(source), None
    if isinstance(source, str) and source.startswith("commit:"):
        sha = source.removeprefix("commit:")
        if COMMIT_PATTERN.fullmatch(sha):
            return "commit", sha
    raise FaultForgeError(
        "invalid_argument",
        f"source must be \"worktree\", \"commit:<sha>\" or \"image\", not {_clean(repr(source), 80)}",
        "A commit is 7 to 64 lowercase hex characters, such as commit:1d9ea55.",
    )


def is_secret(relative: str) -> bool:
    return any(
        fnmatch.fnmatchcase(part.lower(), pattern) for part in PurePosixPath(relative).parts for pattern in SECRET_PATTERNS
    )


def safe_relative(name: str) -> PurePosixPath | None:
    """A plain relative path inside the snapshot, or None. `.git` is never copied."""
    if not name or len(name) > MAX_PATH_CHARS or "\0" in name or "\\" in name:
        return None
    path = PurePosixPath(name)
    if path.is_absolute() or any(part in ("", ".", "..", ".git") for part in path.parts):
        return None
    return path


def _git_env() -> dict[str, str]:
    env = {name: os.environ[name] for name in ("PATH", "HOME") if name in os.environ}
    env.setdefault("PATH", "/usr/local/bin:/usr/bin:/bin")
    env.update({
        "GIT_TERMINAL_PROMPT": "0", "GIT_OPTIONAL_LOCKS": "0", "GIT_NO_LAZY_FETCH": "1",
        "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull, "LC_ALL": "C",
    })
    return env


def git(project: Path, *args: str, timeout_s: float = GIT_TIMEOUT_S) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            ["git", "-C", str(project), *GIT_ARGS, *args], env=_git_env(), stdin=subprocess.DEVNULL,
            capture_output=True, timeout=timeout_s, check=False,
        )
    except subprocess.TimeoutExpired as error:
        raise FaultForgeError("snapshot_failed", f"git {args[0]} took longer than {timeout_s:g} s") from error
    except OSError as error:
        raise FaultForgeError("snapshot_failed", f"can't run git: {type(error).__name__}", "Install git.") from error


def require_git_repo(project: Path) -> None:
    result = git(project, "rev-parse", "--is-inside-work-tree")
    if result.returncode != 0 or result.stdout.strip() != b"true":
        raise FaultForgeError(
            "not_a_git_repo",
            f"{project} isn't inside a git working tree",
            "Snapshots come from git. Run `git init` and commit the project, or pick a project that is a git repo.",
        )


def _inside(path: Path, parents: tuple[Path, ...]) -> bool:
    return any(path == parent or path.is_relative_to(parent) for parent in parents)


def _refused(project: Path, reason: str) -> FaultForgeError:
    return FaultForgeError(
        "project_not_allowed",
        f"{project}: {reason}",
        "The snapshot reads only a repository that lives with the project inside an allowed root, with its own "
        "objects. Clone the project plainly (no .git file, alternates or partial clone) inside the root.",
    )


def require_repo_inside(project: Path, roots: tuple[Path, ...]) -> Path:
    """The repository that git resolves for project must be the project's own, so that the object store
    the snapshot reads stays inside the allowed roots. Returns the common dir."""
    require_git_repo(project)
    result = git(project, "rev-parse", "--absolute-git-dir", "--git-common-dir", "--show-toplevel")
    lines = result.stdout.decode("utf-8", "surrogateescape").split("\n")
    if result.returncode != 0 or len(lines) < 3 or not all(lines[:3]):
        raise FaultForgeError("snapshot_failed", f"git rev-parse failed: {_clean(result.stderr.decode('utf-8', 'replace'))}")
    git_dir, common, toplevel = ((project / line).resolve() for line in lines[:3])
    if not (project == toplevel or project.is_relative_to(toplevel)):
        raise _refused(project, "git's working tree for it isn't the project or one of its parents")
    within = (toplevel, *roots)
    if not _inside(git_dir, within):
        raise _refused(project, "its .git points at a repository outside the project and the allowed roots")
    if not _inside(common, within):
        raise _refused(project, "its git worktree belongs to a repository outside the project and the allowed roots")
    for name in ("alternates", "http-alternates"):
        if (common / "objects" / "info" / name).exists():
            raise _refused(project, f"its repository borrows objects through objects/info/{name}")
    promisor = git(project, "config", "--local", "--get-regexp", PROMISOR_CONFIG)
    if promisor.returncode == 0 and promisor.stdout.strip():
        raise _refused(project, "its repository is a partial (promisor) clone, which would fetch objects on demand")
    if promisor.returncode not in (0, 1):
        raise FaultForgeError("snapshot_failed", f"git config failed: {_clean(promisor.stderr.decode('utf-8', 'replace'))}")
    pack = common / "objects" / "pack"
    if pack.is_dir() and any(entry.name.endswith(".promisor") for entry in pack.iterdir()):
        raise _refused(project, "its repository has promisor packs, which would fetch objects on demand")
    return common


class _Writer:
    """Counts against the caps before each file is written. A secret is never written at all."""

    def __init__(self, snapshot: Snapshot, max_bytes: int, max_files: int):
        self.snapshot = snapshot
        self.max_bytes = max_bytes
        self.max_files = max_files

    def admit(self, relative: PurePosixPath, size: int) -> bool:
        if is_secret(relative.as_posix()):
            self.snapshot.dropped_secrets.append(relative.as_posix())
            return False
        if self.snapshot.files + 1 > self.max_files or self.snapshot.bytes + size > self.max_bytes:
            raise FaultForgeError(
                "snapshot_too_large",
                f"the snapshot is larger than {self.max_files} files or {self.max_bytes // (1024 * 1024)} MB",
                "Point project_dir at the service's own directory, or add build output and dependencies to .gitignore.",
            )
        return True

    def write(self, relative: PurePosixPath, source: IO[bytes], size: int) -> None:
        destination = self.snapshot.root.joinpath(*relative.parts)
        for parent in reversed(destination.relative_to(self.snapshot.root).parents[:-1]):
            directory = self.snapshot.root / parent
            if directory.is_symlink() or (directory.exists() and not directory.is_dir()):
                raise FaultForgeError("snapshot_failed", f"{relative} collides with another entry")
            directory.mkdir(mode=0o700, exist_ok=True)
        written = 0
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
        with os.fdopen(os.open(destination, flags, 0o600), "wb") as out:
            while chunk := source.read(min(CHUNK, size - written + 1)):
                written += len(chunk)
                if written > size:
                    raise FaultForgeError("snapshot_failed", f"{relative} changed while it was copied")
                out.write(chunk)
        self.snapshot.files += 1
        self.snapshot.bytes += written


def new_snapshot(state: Path, source: str, commit: str | None = None) -> Snapshot:
    parent = private_dir(state / "snapshots")
    return Snapshot(root=Path(tempfile.mkdtemp(prefix=f"{secrets.token_hex(4)}-", dir=parent)), source=source, commit=commit)


def _copy_file(project: Path, relative: PurePosixPath, writer: _Writer) -> None:
    """Regular files only, and only when no component of the path is a symlink."""
    path = project.joinpath(*relative.parts)
    try:
        info = path.lstat()
    except FileNotFoundError:
        return  # Tracked but deleted in the working tree.
    if not stat.S_ISREG(info.st_mode) or os.path.realpath(path) != str(path):
        writer.snapshot.skipped.append(relative.as_posix())
        return
    if not writer.admit(relative, info.st_size):
        return
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError:
        writer.snapshot.skipped.append(relative.as_posix())
        return
    with os.fdopen(descriptor, "rb") as source:
        # The same file that was checked: a parent swapped for a symlink in between opens another one.
        opened = os.fstat(source.fileno())
        if (
            not stat.S_ISREG(opened.st_mode)
            or (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino)
            or os.path.realpath(path) != str(path)
        ):
            writer.snapshot.skipped.append(relative.as_posix())
            return
        writer.write(relative, source, info.st_size)


def _listing(project: Path) -> list[str]:
    result = git(project, "ls-files", "--cached", "--others", "--exclude-standard", "-z")
    if result.returncode != 0:
        raise FaultForgeError("snapshot_failed", f"git ls-files failed: {_clean(result.stderr.decode('utf-8', 'replace'))}")
    if len(result.stdout) > MAX_LISTING_BYTES:
        raise FaultForgeError("snapshot_too_large", "git lists too many files", "Point project_dir at a smaller directory.")
    return list(dict.fromkeys(name for name in result.stdout.decode("utf-8", "surrogateescape").split("\0") if name))


def snapshot_worktree(
    project: Path, state: Path, roots: tuple[Path, ...], max_bytes: int = MAX_SNAPSHOT_BYTES, max_files: int = MAX_SNAPSHOT_FILES,
) -> Snapshot:
    """git lists paths relative to project_dir and only below it, which is what a subdirectory needs."""
    require_repo_inside(project, roots)
    snapshot = new_snapshot(state, "worktree")
    try:
        writer = _Writer(snapshot, max_bytes, max_files)
        for name in _listing(project):
            relative = safe_relative(name)
            if relative is None:
                snapshot.skipped.append(_clean(name, 120))
                continue
            _copy_file(project, relative, writer)
    except BaseException:
        snapshot.remove()
        raise
    return snapshot


def copy_files(project: Path, state: Path, names: list[str], source: str) -> Snapshot:
    """Named files only, such as a stack's configs when its code comes from a prebuilt image."""
    snapshot = new_snapshot(state, source)
    try:
        writer = _Writer(snapshot, MAX_SNAPSHOT_BYTES, MAX_SNAPSHOT_FILES)
        for name in names:
            relative = safe_relative(name)
            if relative is not None:
                _copy_file(project, relative, writer)
    except BaseException:
        snapshot.remove()
        raise
    return snapshot


def resolve_commit(project: Path, sha: str) -> str:
    if not COMMIT_PATTERN.fullmatch(sha):
        raise FaultForgeError("invalid_argument", "a commit must be 7 to 64 lowercase hex characters")
    result = git(project, "rev-parse", "--verify", "--quiet", "--end-of-options", f"{sha}^{{commit}}")
    full = result.stdout.decode("ascii", "replace").strip()
    if result.returncode != 0 or not FULL_COMMIT_PATTERN.fullmatch(full):
        raise FaultForgeError("invalid_argument", f"{sha} isn't a commit in this repo", "Pass a commit from `git log`.")
    return full


TREE_ENTRY = re.compile(rb"(\d{6}) (\w+) ([0-9a-f]{40}|[0-9a-f]{64})\t(.*)", re.DOTALL)
REGULAR_MODES = (b"100644", b"100755")


def _tree(project: Path, commit: str) -> list[tuple[str, bytes, str]]:
    """ls-tree run in a subdirectory lists that subdirectory, with paths relative to it."""
    result = git(project, "ls-tree", "-r", "-z", "--end-of-options", commit)
    if result.returncode != 0:
        raise FaultForgeError("snapshot_failed", f"git ls-tree failed: {_clean(result.stderr.decode('utf-8', 'replace'))}")
    if len(result.stdout) > MAX_LISTING_BYTES:
        raise FaultForgeError("snapshot_too_large", "the commit lists too many files", "Point project_dir at a smaller directory.")
    entries = []
    for line in result.stdout.split(b"\0"):
        match = TREE_ENTRY.fullmatch(line)
        if match is not None:
            mode, kind, oid, name = match.groups()
            entries.append((name.decode("utf-8", "surrogateescape"), mode if kind == b"blob" else b"", oid.decode("ascii")))
    return entries


class _Blobs:
    """`cat-file --batch` prints raw objects: no filters, no attributes, no textconv. A watchdog kills
    it at the deadline, so a stuck git can't hold the run lock."""

    def __init__(self, project: Path, deadline_s: float):
        self.process = subprocess.Popen(
            ["git", "-C", str(project), *GIT_ARGS, "cat-file", "--batch"], env=_git_env(),
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        )
        self.timer = threading.Timer(deadline_s, self.process.kill)
        self.timer.start()

    def open(self, oid: str) -> int:
        self.process.stdin.write(oid.encode("ascii") + b"\n")
        self.process.stdin.flush()
        header = self.process.stdout.readline(256).split()
        if len(header) != 3 or header[0].decode("ascii", "replace") != oid or header[1] != b"blob":
            raise FaultForgeError("snapshot_failed", "git cat-file stopped or returned something other than the blob")
        return int(header[2])

    def skip(self, size: int) -> None:
        while size > 0:
            chunk = self.process.stdout.read(min(CHUNK, size))
            if not chunk:
                raise FaultForgeError("snapshot_failed", "git cat-file output was cut short")
            size -= len(chunk)
        self.process.stdout.read(1)

    def close(self) -> None:
        self.timer.cancel()
        if self.process.poll() is None:
            self.process.kill()
        self.process.wait()
        for stream in (self.process.stdin, self.process.stdout):
            try:
                stream.close()
            except OSError:
                pass


class _Bounded:
    def __init__(self, stream: IO[bytes], size: int):
        self.stream, self.left = stream, size

    def read(self, count: int) -> bytes:
        chunk = self.stream.read(min(count, self.left)) if self.left > 0 else b""
        self.left -= len(chunk)
        return chunk


def snapshot_commit(
    project: Path, state: Path, sha: str, roots: tuple[Path, ...], max_bytes: int = MAX_SNAPSHOT_BYTES,
    max_files: int = MAX_SNAPSHOT_FILES,
) -> Snapshot:
    """Links and submodules are skipped, and a path with .. or .git never reaches the disk."""
    require_repo_inside(project, roots)
    full = resolve_commit(project, sha)
    snapshot = new_snapshot(state, f"commit:{full}", full)
    try:
        writer = _Writer(snapshot, max_bytes, max_files)
        blobs = _Blobs(project, GIT_TIMEOUT_S * 2)
        try:
            for name, mode, oid in _tree(project, full):
                relative = safe_relative(name)
                if relative is None or mode not in REGULAR_MODES:
                    snapshot.skipped.append(_clean(name, 120))
                    continue
                size = blobs.open(oid)
                if not writer.admit(relative, size):
                    blobs.skip(size)
                    continue
                writer.write(relative, _Bounded(blobs.process.stdout, size), size)
                if blobs.process.stdout.read(1) != b"\n":
                    raise FaultForgeError("snapshot_failed", "git cat-file output was cut short")
        finally:
            blobs.close()
    except BaseException:
        snapshot.remove()
        raise
    return snapshot
