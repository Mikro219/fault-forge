"""M8: the MCP server. The allowlist, snapshots, run ids, the busy lock and structured errors are
tested without Docker. One test drives the real server over stdio, and one docker test runs the
Ticket API end to end through it."""

import asyncio
import json
import os
import shutil
import subprocess
import sys
import threading
from pathlib import Path

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from faultforge.errors import FaultForgeError
from faultforge.mcp_server import settings as settings_module, snapshot, store
from faultforge.mcp_server.tools import Tools
from faultforge.models import Report
from faultforge.sandbox import ContainmentFacts, SandboxResult, TargetSpec

ROOT = Path(__file__).resolve().parents[1]
DEMO = ROOT / "demo"
TICKET_API = DEMO / "ticket-api"
FAIL_REPORT = ROOT / "tests" / "fixtures" / "report_fail.json"
SCENARIO = "last-ticket-rush"
TOOL_NAMES = {
    "faultforge_status", "faultforge_list_scenarios", "faultforge_validate_scenario",
    "faultforge_run_scenario", "faultforge_get_run",
}


def git(repo: Path, *args: str) -> str:
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(repo), "GIT_CONFIG_NOSYSTEM": "1"}
    return subprocess.run(
        ["git", "-C", str(repo), "-c", "user.email=t@example.invalid", "-c", "user.name=t", *args],
        check=True, capture_output=True, env=env, text=True,
    ).stdout.strip()


SPORTSDECK_IMAGE = "faultforge-sportsdeck:buggy"


def configure(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, roots: list[str] | None, images: list[str] = ()) -> Path:
    config = tmp_path / "mcp.toml"
    if roots is not None:
        config.write_text(
            f"allowed_roots = {json.dumps(roots)}\nallowed_images = {json.dumps(list(images))}\n", encoding="utf-8",
        )
    state = tmp_path / "state"
    monkeypatch.setenv(settings_module.CONFIG_ENV, str(config))
    monkeypatch.setenv(settings_module.STATE_ENV, str(state))
    return state


@pytest.fixture
def project(tmp_path: Path) -> Path:
    """A git repo shaped like the Ticket API, in a subdirectory, as a real monorepo would be."""
    repo = tmp_path / "repo"
    service = repo / "service"
    shutil.copytree(TICKET_API, service, ignore=shutil.ignore_patterns("__pycache__", "false_fixes", "reference_fix"))
    (repo / ".gitignore").write_text("ignored.txt\nbuild/\n", encoding="utf-8")
    git(repo, "init", "-q")
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "initial")
    return service


def fake_result(work_dir: Path, run_id: str = "run_20261106_071900_ab12") -> SandboxResult:
    """Writes what a runner would have written into its /out, then answers like run_sandboxed."""
    report = Report.model_validate_json(FAIL_REPORT.read_text(encoding="utf-8")).model_copy(update={"run_id": run_id})
    directory = work_dir / ".faultforge" / "runs" / run_id
    directory.mkdir(parents=True)
    (directory / "report.json").write_text(report.model_dump_json(), encoding="utf-8")
    records = [
        {"trial": 1 + i % 2, "request_id": f"r{i}", "vu": i + 1, "iter": 0, "t_start": 1000.0 + i, "t_end": 1050.0 + i,
         "latency_ms": 50.0, "status": 200 if i % 3 else 409, "outcome": "accepted" if i % 3 else "rejected",
         "captured": f"order-{i}\x1b[31m"}
        for i in range(7)
    ]
    (directory / "requests.jsonl").write_text("".join(json.dumps(item) + "\n" for item in records) + "not json\n", encoding="utf-8")
    return SandboxResult(
        status="completed", report=report, deployed_code_sha256="ab" * 32,
        containment=ContainmentFacts(containers_left=0, wall_s=1.0, wall_limit_s=100.0),
    )


class Recorder:
    def __init__(self, before_return=None):
        self.calls: list[tuple[TargetSpec, Path, str, object, list[str]]] = []
        self.before_return = before_return

    def __call__(self, spec: TargetSpec, work_dir: Path, scenario_id: str, options) -> SandboxResult:
        snapshot_root = Path(spec.config_path).parent
        files = sorted(path.relative_to(snapshot_root).as_posix() for path in snapshot_root.rglob("*") if path.is_file())
        self.calls.append((spec, work_dir, scenario_id, options, files))
        if self.before_return is not None:
            self.before_return()
        return fake_result(work_dir)


# The project-root allowlist


def test_a_missing_config_allows_nothing(monkeypatch, tmp_path, project) -> None:
    configure(monkeypatch, tmp_path, None)
    result = Tools().list_scenarios(str(project))
    assert result["status"] == "error" and result["error_class"] == "project_not_allowed"
    assert str(tmp_path / "mcp.toml") in result["fix_hint"]
    assert Tools().status()["allowed_roots"] == []


def test_a_malformed_config_allows_nothing(monkeypatch, tmp_path, project) -> None:
    configure(monkeypatch, tmp_path, [str(project)])
    (tmp_path / "mcp.toml").write_text(f'allowed_roots = ["{project}"]\nextra = 1\n', encoding="utf-8")
    assert Tools().list_scenarios(str(project))["error_class"] == "project_not_allowed"
    assert "unknown keys" in Tools().status()["config_problems"][0]


def test_an_allowed_project_lists_its_scenarios_without_contacting_it(monkeypatch, tmp_path, project) -> None:
    configure(monkeypatch, tmp_path, [str(project.parent)])
    result = Tools().list_scenarios(str(project))
    assert result["status"] == "ok" and result["kind"] == "python"
    assert [item["id"] for item in result["scenarios"]] == [SCENARIO, "plenty-of-stock", "sold-out"]


def test_dot_dot_and_symlinks_cant_leave_a_root(monkeypatch, tmp_path, project) -> None:
    configure(monkeypatch, tmp_path, [str(project)])
    outside = tmp_path / "outside"
    shutil.copytree(project, outside)
    (project / "escape").symlink_to(outside, target_is_directory=True)
    for path in (f"{project}/../../outside", str(project / "escape"), str(project.parent)):
        assert Tools().validate_scenario(path)["error_class"] == "project_not_allowed", path
    assert Tools().validate_scenario("service")["error_class"] == "invalid_argument"


def test_a_symlinked_root_and_the_filesystem_root(monkeypatch, tmp_path, project) -> None:
    link = tmp_path / "link"
    link.symlink_to(project.parent, target_is_directory=True)
    configure(monkeypatch, tmp_path, ["/", str(link)])
    loaded = settings_module.load_settings()
    assert loaded.allowed_roots == (project.parent.resolve(),)
    assert any("filesystem root" in problem for problem in loaded.problems)
    assert Tools().validate_scenario(str(link / "service"))["valid"] is True
    assert Tools().validate_scenario(str(tmp_path))["error_class"] == "project_not_allowed"


# Snapshots


def test_worktree_snapshot_rules(tmp_path, project) -> None:
    (project / "app.py").write_text("# edited, not committed\n", encoding="utf-8")
    (project / "new_untracked.py").write_text("x = 1\n", encoding="utf-8")
    (project / "ignored.txt").write_text("ignored\n", encoding="utf-8")
    (project / "build").mkdir()
    (project / "build" / "out.js").write_text("ignored\n", encoding="utf-8")
    secrets = (
        ".env", ".env.local", "server.pem", "id_rsa", "id_rsa.pub", "deploy.key", "id_ed25519", "id_ed25519.pub", "id_ecdsa",
        "client.p12", "client.pfx", ".npmrc", ".netrc", ".pypirc",
    )
    for name in secrets:
        (project / name).write_text("secret\n", encoding="utf-8")
    (project / "conf").mkdir()
    (project / "conf" / ".env.production").write_text("secret\n", encoding="utf-8")
    (project / "conf" / "deep").mkdir()
    (project / "conf" / "deep" / ".npmrc").write_text("//registry/:_authToken=x\n", encoding="utf-8")
    secret_outside = tmp_path / "secret.txt"
    secret_outside.write_text("outside\n", encoding="utf-8")
    (project / "link_out.txt").symlink_to(secret_outside)
    (project / "link_in.py").symlink_to(project / "app.py")
    linked_dir = tmp_path / "linked_dir"
    linked_dir.mkdir()
    (linked_dir / "inside.py").write_text("x\n", encoding="utf-8")
    (project / "dir_link").symlink_to(linked_dir, target_is_directory=True)

    snap = snapshot.snapshot_worktree(project, tmp_path / "state", (project.parent,))
    try:
        files = sorted(path.relative_to(snap.root).as_posix() for path in snap.root.rglob("*") if path.is_file() or path.is_symlink())
        assert "new_untracked.py" in files and "app.py" in files and "ticket_support/db.py" in files
        assert (snap.root / "app.py").read_text(encoding="utf-8") == "# edited, not committed\n"
        assert not any(name in files for name in ("ignored.txt", "build/out.js", ".gitignore"))
        assert not any(".env" in name or name.endswith((".pem", ".key", ".p12", ".pfx", "rc")) or "id_" in name for name in files)
        assert not any(name.endswith(".netrc") for name in files)
        assert not any("link" in name or "inside.py" in name for name in files)
        assert not any(path.is_symlink() for path in snap.root.rglob("*"))
        assert not (snap.root / ".git").exists()
        assert len(snap.dropped_secrets) == len(secrets) + 2
        assert {"conf/.env.production", "conf/deep/.npmrc"} <= set(snap.dropped_secrets)
        assert {"link_out.txt", "link_in.py", "dir_link"} <= set(snap.skipped)
        assert snap.root.is_relative_to(tmp_path / "state" / "snapshots")
    finally:
        snap.remove()
    assert not snap.root.exists()


def test_the_size_caps_refuse_and_leave_nothing_behind(tmp_path, project) -> None:
    state = tmp_path / "state"
    for kwargs in ({"max_files": 3}, {"max_bytes": 1000}):
        with pytest.raises(FaultForgeError) as caught:
            snapshot.snapshot_worktree(project, state, (project.parent,), **kwargs)
        assert caught.value.error_class == "snapshot_too_large"
        with pytest.raises(FaultForgeError) as caught:
            snapshot.snapshot_commit(project, state, git(project, "rev-parse", "HEAD"), (project.parent,), **kwargs)
        assert caught.value.error_class == "snapshot_too_large"
    assert list((state / "snapshots").iterdir()) == []


def test_a_commit_snapshot_is_the_commit_without_secrets_or_links(tmp_path, project) -> None:
    secrets = (".env", "cert.pem", "id_ed25519", "id_ecdsa.pub", "store.p12", "store.pfx", ".npmrc", ".netrc", ".pypirc", "deploy.key")
    for name in secrets:
        (project / name).write_text("SECRET=1\n", encoding="utf-8")
    (project / "deep" / "er").mkdir(parents=True)
    (project / "deep" / "er" / ".netrc").write_text("machine x login y password z\n", encoding="utf-8")
    (project / "link.py").symlink_to(project / "app.py")
    git(project, "add", "-f", *secrets, "deep/er/.netrc", "link.py")
    git(project, "commit", "-qm", "secrets")
    sha = git(project, "rev-parse", "HEAD")
    (project / "app.py").write_text("# uncommitted\n", encoding="utf-8")
    snap = snapshot.snapshot_commit(project, tmp_path / "state", sha[:10], (project.parent,))
    try:
        files = sorted(path.relative_to(snap.root).as_posix() for path in snap.root.rglob("*") if path.is_file() or path.is_symlink())
        assert snap.commit == sha and snap.source == f"commit:{sha}"
        assert (snap.root / "app.py").read_bytes() == (TICKET_API / "app.py").read_bytes()
        assert "faultforge.yaml" in files and "ticket_support/server.py" in files
        assert not any(name in files for name in (*secrets, "deep/er/.netrc", "link.py", ".gitignore"))
        assert sorted(snap.dropped_secrets) == sorted([*secrets, "deep/er/.netrc"]) and "link.py" in snap.skipped
    finally:
        snap.remove()


def test_a_commit_snapshot_runs_no_filter_driver_and_reads_no_global_config(tmp_path, project, monkeypatch) -> None:
    """git archive would run a smudge command from the repo's config on the host."""
    marker = tmp_path / "PWNED"
    (project / "app.py").write_text("print('committed')\n", encoding="utf-8")
    # At the repo's root: git archive of a subdirectory honours that file, and would run the filter.
    (project.parent / ".gitattributes").write_text("*.py filter=evil\n", encoding="utf-8")
    git(project, "add", ".")
    git(project, "commit", "-qm", "filtered")
    git(project, "config", "filter.evil.smudge", f"sh -c 'touch {marker}; cat'")
    home = tmp_path / "home"
    home.mkdir()
    (home / ".gitconfig").write_text(f"[filter \"evil\"]\n\tsmudge = sh -c 'touch {marker}; cat'\n", encoding="utf-8")
    monkeypatch.setenv("HOME", str(home))
    snap = snapshot.snapshot_commit(project, tmp_path / "state", git(project, "rev-parse", "HEAD"), (project.parent,))
    try:
        assert (snap.root / "app.py").read_text(encoding="utf-8") == "print('committed')\n"
        assert not marker.exists()
    finally:
        snap.remove()
    assert snapshot._git_env()["GIT_CONFIG_GLOBAL"] == os.devnull and snapshot._git_env()["GIT_NO_LAZY_FETCH"] == "1"
    assert "protocol.allow=never" in snapshot.GIT_ARGS


# The project's git metadata is untrusted: the object store must stay inside the allowed roots.


def outside_repo(tmp_path: Path) -> tuple[Path, str]:
    """A repo outside every root, holding a file the snapshot must never see."""
    outside = tmp_path / "outside_repo"
    outside.mkdir()
    (outside / "secret_app.py").write_text("SECRET = 'outside-the-allowlist'\n", encoding="utf-8")
    (outside / "faultforge.yaml").write_text((TICKET_API / "faultforge.yaml").read_text(encoding="utf-8"), encoding="utf-8")
    git(outside, "init", "-q")
    git(outside, "add", ".")
    git(outside, "commit", "-qm", "outside")
    return outside, git(outside, "rev-parse", "HEAD")


def assert_refused(take, reason: str) -> None:
    with pytest.raises(FaultForgeError) as caught:
        take()
    assert caught.value.error_class == "project_not_allowed", caught.value.message
    assert reason in caught.value.message


def test_a_git_file_pointing_outside_the_root_is_refused(monkeypatch, tmp_path) -> None:
    outside, sha = outside_repo(tmp_path)
    root = tmp_path / "allowed"
    project = root / "proj"
    project.mkdir(parents=True)
    (project / ".git").write_text(f"gitdir: {outside / '.git'}\n", encoding="utf-8")
    roots = (root.resolve(),)
    assert_refused(lambda: snapshot.snapshot_commit(project, tmp_path / "state", sha, roots), ".git points at a repository outside")
    assert_refused(lambda: snapshot.snapshot_worktree(project, tmp_path / "state", roots), ".git points at a repository outside")
    configure(monkeypatch, tmp_path, [str(root)])
    result = Tools(run_sandboxed=lambda *a: pytest.fail("must not run")).run_scenario(str(project), SCENARIO, f"commit:{sha}")
    assert result["error_class"] == "project_not_allowed"
    assert not (tmp_path / "state" / "snapshots").exists() or list((tmp_path / "state" / "snapshots").iterdir()) == []


def test_alternates_pointing_outside_the_root_are_refused(tmp_path) -> None:
    outside, sha = outside_repo(tmp_path)
    root = tmp_path / "allowed"
    project = root / "proj"
    project.mkdir(parents=True)
    git(project, "init", "-q")
    (project / "app.py").write_text("x = 1\n", encoding="utf-8")
    git(project, "add", ".")
    git(project, "commit", "-qm", "own")
    (project / ".git" / "objects" / "info" / "alternates").write_text(f"{outside / '.git' / 'objects'}\n", encoding="utf-8")
    assert git(project, "cat-file", "-t", sha) == "commit"  # git itself can see the borrowed objects
    roots = (root.resolve(),)
    assert_refused(lambda: snapshot.snapshot_commit(project, tmp_path / "state", sha, roots), "objects/info/alternates")
    assert_refused(lambda: snapshot.snapshot_worktree(project, tmp_path / "state", roots), "objects/info/alternates")
    (project / ".git" / "objects" / "info" / "alternates").unlink()
    (project / ".git" / "objects" / "info" / "http-alternates").write_text("https://example.invalid/objects\n", encoding="utf-8")
    assert_refused(lambda: snapshot.snapshot_worktree(project, tmp_path / "state", roots), "objects/info/http-alternates")
    assert "GIT_ALTERNATE_OBJECT_DIRECTORIES" not in snapshot._git_env()


def test_a_git_worktree_inside_the_root_works_and_one_outside_is_refused(tmp_path, project) -> None:
    """A normal repo and a `git worktree add` worktree whose main repo is in the root both snapshot."""
    root = project.parent.parent  # tmp_path: holds the repo and its worktree
    worktree = root / "wt"
    git(project, "worktree", "add", "-q", str(worktree))
    (worktree / "service" / "app.py").write_text("# from the worktree\n", encoding="utf-8")
    sha = git(project, "rev-parse", "HEAD")
    for target in (project, worktree / "service"):
        snap = snapshot.snapshot_worktree(target, tmp_path / "state", (root.resolve(),))
        try:
            assert (snap.root / "app.py").is_file() and (snap.root / "faultforge.yaml").is_file()
        finally:
            snap.remove()
        snap = snapshot.snapshot_commit(target, tmp_path / "state", sha, (root.resolve(),))
        try:
            assert (snap.root / "app.py").read_bytes() == (TICKET_API / "app.py").read_bytes()
        finally:
            snap.remove()
    snap = snapshot.snapshot_worktree(worktree / "service", tmp_path / "state", (root.resolve(),))
    try:
        assert (snap.root / "app.py").read_text(encoding="utf-8") == "# from the worktree\n"
    finally:
        snap.remove()
    # Only the worktree is allowed: its common dir lives in the main repo, outside the root.
    only_worktree = (worktree.resolve(),)
    assert_refused(lambda: snapshot.snapshot_worktree(worktree / "service", tmp_path / "state", only_worktree), "outside the project and the allowed roots")
    assert_refused(lambda: snapshot.snapshot_commit(worktree / "service", tmp_path / "state", sha, only_worktree), "outside the project and the allowed roots")
    # The common dir check stands on its own: a worktree whose own git dir is a plain .git but whose
    # common dir points elsewhere (a `commondir` file) is refused as well.
    git_dir = Path(git(worktree / "service", "rev-parse", "--absolute-git-dir"))
    assert (git_dir / "commondir").is_file()
    main_git = Path(git(project, "rev-parse", "--absolute-git-dir"))
    (worktree / ".git").unlink()
    shutil.copytree(git_dir, worktree / ".git")
    (worktree / ".git" / "commondir").write_text(f"{main_git}\n", encoding="utf-8")
    assert git(worktree / "service", "rev-parse", "--git-common-dir") == str(main_git)
    assert_refused(lambda: snapshot.snapshot_worktree(worktree / "service", tmp_path / "state", only_worktree), "worktree belongs to")


def test_a_promisor_repo_is_refused_and_its_ssh_command_never_runs(tmp_path) -> None:
    """A partial clone would fetch a missing blob on demand through the remote the repo configures, and
    core.sshCommand from the repo's config would then run on the host."""
    marker = tmp_path / "PWNED_SSH"
    repo = tmp_path / "repo"
    repo.mkdir()
    for name in ("app.py", "faultforge.yaml"):
        shutil.copy(TICKET_API / name, repo / name)
    git(repo, "init", "-q")
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "one")
    missing = subprocess.run(["git", "-C", repo, "hash-object", "--stdin"], input="x = 1\n", capture_output=True, text=True, check=True).stdout.strip()
    listing = "".join(
        f"100644 blob {git(repo, 'rev-parse', f'HEAD:{name}')}\t{name}\n" for name in ("app.py", "faultforge.yaml")
    ) + f"100644 blob {missing}\tmissing.py\n"
    tree = subprocess.run(["git", "-C", repo, "mktree", "--missing"], input=listing, capture_output=True, text=True, check=True).stdout.strip()
    sha = git(repo, "commit-tree", "-p", "HEAD", "-m", "two", tree)
    git(repo, "update-ref", "HEAD", sha)
    git(repo, "config", "extensions.partialClone", "origin")
    git(repo, "config", "remote.origin.promisor", "true")
    git(repo, "config", "remote.origin.url", "ssh://attacker.invalid/repo")
    git(repo, "config", "core.sshCommand", f"sh -c 'touch {marker}; exit 1'")
    roots = (tmp_path.resolve(),)
    assert_refused(lambda: snapshot.snapshot_commit(repo, tmp_path / "state", sha, roots), "partial (promisor) clone")
    assert_refused(lambda: snapshot.snapshot_worktree(repo, tmp_path / "state", roots), "partial (promisor) clone")
    assert not marker.exists()
    # Belt and braces: even if the check were skipped, no transport may run. GIT_NO_LAZY_FETCH stays set,
    # and protocol.allow=never means git has no protocol to fetch with.
    result = subprocess.run(
        ["git", "-C", str(repo), *snapshot.GIT_ARGS, "cat-file", "--batch"], input=f"{missing}\n".encode(),
        env=snapshot._git_env(), capture_output=True, timeout=60,
    )
    assert result.stdout.strip().endswith(b"missing") and not marker.exists()
    # The promisor pack marker is caught too, without any config.
    plain = tmp_path / "plain"
    shutil.copytree(repo, plain)
    for key in ("extensions.partialClone", "remote.origin.promisor", "remote.origin.url", "core.sshCommand"):
        git(plain, "config", "--unset", key)
    (plain / ".git" / "objects" / "pack").mkdir(exist_ok=True)
    (plain / ".git" / "objects" / "pack" / "pack-abc.promisor").write_text("", encoding="utf-8")
    assert_refused(lambda: snapshot.snapshot_worktree(plain, tmp_path / "state", roots), "promisor packs")


def test_the_git_environment_is_rebuilt(monkeypatch) -> None:
    for name in ("GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_OBJECT_DIRECTORY", "GIT_DIR", "GIT_WORK_TREE", "GIT_SSH_COMMAND", "GIT_CONFIG_PARAMETERS"):
        monkeypatch.setenv(name, "/evil")
    env = snapshot._git_env()
    assert set(env) <= {"PATH", "HOME", "GIT_TERMINAL_PROMPT", "GIT_OPTIONAL_LOCKS", "GIT_NO_LAZY_FETCH", "GIT_CONFIG_NOSYSTEM", "GIT_CONFIG_GLOBAL", "LC_ALL"}
    assert env["GIT_NO_LAZY_FETCH"] == "1" and "GIT_ALTERNATE_OBJECT_DIRECTORIES" not in env


def test_stale_snapshots_and_work_dirs_are_swept(tmp_path) -> None:
    state = tmp_path / "state"
    old, fresh = state / "snapshots" / "old", state / "work" / "fresh"
    old.mkdir(parents=True)
    fresh.mkdir(parents=True)
    os.utime(old, (0, 0))
    assert store.sweep_stale(state, now=store.STALE_AFTER_S + 10) == 1
    assert not old.exists() and fresh.exists()


@pytest.mark.parametrize("source", ["commit:-rf", "commit:--output=/tmp/x", "commit:HEAD", "commit:ABCDEF1", "commit:abc", "head", "", None, 7])
def test_bad_sources_are_refused(source) -> None:
    with pytest.raises(FaultForgeError) as caught:
        snapshot.parse_source(source)
    assert caught.value.error_class == "invalid_argument"


def test_an_unknown_commit_and_a_plain_directory(tmp_path, project) -> None:
    with pytest.raises(FaultForgeError) as caught:
        snapshot.snapshot_commit(project, tmp_path / "state", "deadbeefdeadbeef", (project.parent,))
    assert caught.value.error_class == "invalid_argument"
    plain = tmp_path / "plain"
    shutil.copytree(project, plain)
    for take in (lambda: snapshot.snapshot_worktree(plain, tmp_path / "state", (tmp_path,)),
                 lambda: snapshot.snapshot_commit(plain, tmp_path / "state", "deadbeef", (tmp_path,))):
        with pytest.raises(FaultForgeError) as caught:
            take()
        assert caught.value.error_class == "not_a_git_repo"


def test_secret_names() -> None:
    for name in (
        ".env", ".ENV.local", "a/b/.env.prod", "x.pem", "keys/x.key", "id_rsa", "id_rsa.pub", ".env/inner.txt",
        "id_ed25519", ".ssh/id_ed25519.pub", "id_ecdsa", "a/id_ecdsa-sk", "certs/x.p12", "x.PFX", ".npmrc", "a/b/.npmrc",
        ".netrc", "home/.pypirc", "deep/er/.env.production",
    ):
        assert snapshot.is_secret(name), name
    for name in ("environment.py", "app.py", "keyboard.py", "pem.txt", "my.env.example.py", "npmrc.md", "netrc.py", "idea.py"):
        assert not snapshot.is_secret(name), name


# Runs through the tools, with the sandbox faked


def test_a_run_uses_the_snapshot_and_is_stored(monkeypatch, tmp_path, project) -> None:
    state = configure(monkeypatch, tmp_path, [str(project.parent)])
    (project / "app.py").write_text((TICKET_API / "app.py").read_text(encoding="utf-8") + "# worktree edit\n", encoding="utf-8")
    (project / ".env").write_text("VULTR_INFERENCE_API_KEY=canary\n", encoding="utf-8")
    recorder = Recorder()
    result = Tools(run_sandboxed=recorder).run_scenario(str(project), SCENARIO, "worktree")
    assert result["status"] == "ok", result
    assert result["verdict"] == "FAIL" and result["run_id"] == "run_20261106_071900_ab12"
    assert result["proven"][0].startswith("V1 ") and result["suspected"][0].startswith("H1 ")
    assert "trials complete" in result["completeness"]
    assert result["source"]["source"] == "worktree" and result["source"]["dropped_secrets"] == 1
    spec, work_dir, scenario_id, options, files = recorder.calls[0]
    assert spec.app_source.endswith(b"# worktree edit\n") and spec.support_dir.name == "ticket_support"
    assert not spec.allow_hostile and spec.config_path.is_relative_to(state / "snapshots")
    assert ".env" not in files and "faultforge.yaml" in files
    assert work_dir.is_relative_to(state / "work") and scenario_id == SCENARIO and options.compare_to is None
    # The snapshot and the work dir are gone; the run is stored.
    assert list((state / "snapshots").iterdir()) == [] and list((state / "work").iterdir()) == []
    assert (state / "runs" / result["run_id"] / "report.json").is_file()
    assert len(json.dumps(result, indent=1).splitlines()) <= 60

    tools = Tools()
    assert tools.get_run(result["run_id"])["verdict"] == "FAIL"
    violations = tools.get_run(result["run_id"], "violations")
    assert violations["total"] == len(violations["items"]) >= 1
    page = tools.get_run(result["run_id"], "evidence", offset=1, limit=3)
    assert page["total"] == 7 and page["invalid_lines"] == 1 and len(page["items"]) == 3 and page["next_offset"] == 4
    assert all("\x1b" not in line for line in page["items"])
    rejected = tools.get_run(result["run_id"], "evidence", outcome="rejected", trial=1)
    assert rejected["total"] == 2 and all(" rejected " in line for line in rejected["items"])
    assert tools.get_run(result["run_id"], "performance")["status"] == "ok"
    assert tools.get_run(result["run_id"], "hypotheses")["items"][0]["id"] == "H1"
    assert tools.get_run(result["run_id"], "evidence", limit=101)["error_class"] == "invalid_argument"
    assert tools.get_run(result["run_id"], "secrets")["error_class"] == "invalid_argument"


def test_compare_to_stages_only_the_baseline_report(monkeypatch, tmp_path, project) -> None:
    state = configure(monkeypatch, tmp_path, [str(project.parent)])
    first = Tools(run_sandboxed=Recorder()).run_scenario(str(project), SCENARIO, "worktree")
    seen = {}

    def candidate(spec, work_dir, scenario_id, options):
        seen["baseline"] = sorted(path.name for path in (work_dir / ".faultforge" / "runs").rglob("*"))
        seen["compare_to"] = options.compare_to
        return fake_result(work_dir, "run_20261106_072000_cd34")

    second = Tools(run_sandboxed=candidate).run_scenario(str(project), SCENARIO, "worktree", compare_to=first["run_id"])
    assert second["status"] == "ok" and seen == {"baseline": ["report.json", first["run_id"]], "compare_to": first["run_id"]}
    other = Tools(run_sandboxed=candidate).run_scenario(str(project), "sold-out", "worktree", compare_to=first["run_id"])
    assert other["error_class"] == "not_comparable"
    assert Tools().run_scenario(str(project), SCENARIO, "worktree", compare_to="../runs")["error_class"] == "run_not_found"
    assert (state / "runs" / second["run_id"]).is_dir()


@pytest.mark.parametrize("run_id", ["../state", "run_1", "run_20260101_000000_zzzz", "run_20260101_000000_ab12/..", "", 5])
def test_run_ids_are_checked_before_any_path_is_built(monkeypatch, tmp_path, run_id) -> None:
    state = configure(monkeypatch, tmp_path, [])
    result = Tools().get_run(run_id)
    assert result["status"] == "error" and result["error_class"] in ("run_not_found", "invalid_argument")
    assert not (state / "state").exists()


def test_an_unknown_run(monkeypatch, tmp_path) -> None:
    configure(monkeypatch, tmp_path, [])
    assert Tools().get_run("run_20260101_000000_ab12")["error_class"] == "run_not_found"


def test_a_second_run_while_one_is_running_is_busy(monkeypatch, tmp_path, project) -> None:
    configure(monkeypatch, tmp_path, [str(project.parent)])
    started, release = threading.Event(), threading.Event()

    def hold() -> None:
        started.set()
        assert release.wait(10)

    tools = Tools(run_sandboxed=Recorder(before_return=hold))
    results = {}
    worker = threading.Thread(target=lambda: results.setdefault("first", tools.run_scenario(str(project), SCENARIO, "worktree")))
    worker.start()
    try:
        assert started.wait(10)
        assert tools.status()["busy"] is True
        assert tools.run_scenario(str(project), SCENARIO, "worktree")["error_class"] == "busy"
    finally:
        release.set()
        worker.join(10)
    assert results["first"]["status"] == "ok"
    assert tools.status()["busy"] is False


def test_a_second_server_process_sharing_the_state_dir_is_busy(monkeypatch, tmp_path, project) -> None:
    """Two Tools objects stand for two server processes: each has its own in-process lock, so only the
    flock on <state>/run.lock can tell the second one to wait."""
    state = configure(monkeypatch, tmp_path, [str(project.parent)])
    started, release = threading.Event(), threading.Event()

    def hold() -> None:
        started.set()
        assert release.wait(10)

    first = Tools(run_sandboxed=Recorder(before_return=hold))
    second = Tools(run_sandboxed=lambda spec, work_dir, scenario_id, options: fake_result(work_dir, "run_20261106_072000_cd34"))
    results = {}
    worker = threading.Thread(target=lambda: results.setdefault("first", first.run_scenario(str(project), SCENARIO, "worktree")))
    worker.start()
    try:
        assert started.wait(10)
        assert (state / "run.lock").is_file()
        assert second.status()["busy"] is True
        assert second.run_scenario(str(project), SCENARIO, "worktree")["error_class"] == "busy"
        # A real second process sees the same lock.
        probe = subprocess.run(
            [sys.executable, "-c", "import fcntl,sys\nf=open(sys.argv[1])\ntry:\n fcntl.flock(f,fcntl.LOCK_EX|fcntl.LOCK_NB)\n print('free')\nexcept OSError:\n print('held')",
             str(state / "run.lock")], capture_output=True, text=True, timeout=30,
        )
        assert probe.stdout.strip() == "held"
    finally:
        release.set()
        worker.join(10)
    assert results["first"]["status"] == "ok"
    assert second.status()["busy"] is False
    second_result = second.run_scenario(str(project), SCENARIO, "worktree")
    assert second_result["status"] == "ok"
    # Released on the failure path as well.
    assert Tools(run_sandboxed=lambda *a: 1 / 0).run_scenario(str(project), SCENARIO, "worktree")["error_class"] == "engine_failed"
    assert second.status()["busy"] is False


def test_list_scenarios_pages_twenty_scenarios(monkeypatch, tmp_path, project) -> None:
    configure(monkeypatch, tmp_path, [str(project.parent)])
    config = project / "faultforge.yaml"
    text = config.read_text(encoding="utf-8")
    head, _, rest = text.partition("scenarios:\n")
    block = rest.split("\n  - id: ")[1]  # the first scenario's body
    scenarios = "".join(f"  - id: scenario-{index:02d}\n" + block.split("\n", 1)[1].rstrip("\n") + "\n" for index in range(20))
    config.write_text(head + "scenarios:\n" + scenarios, encoding="utf-8")
    tools = Tools()
    first = tools.list_scenarios(str(project))
    assert first["status"] == "ok", first
    assert [item["id"] for item in first["scenarios"]] == [f"scenario-{index:02d}" for index in range(6)]
    assert first["scenarios_total"] == 20 and first["more"] == 14 and first["next_offset"] == 6
    assert first["scenarios"][0]["flags"] == "mutates_data, user_confirmed" and "20 vus" in first["scenarios"][0]["load"]
    assert len(json.dumps(first, indent=1).splitlines()) <= 60
    last = tools.list_scenarios(str(project), offset=18)
    assert [item["id"] for item in last["scenarios"]] == ["scenario-18", "scenario-19"] and "more" not in last
    assert tools.list_scenarios(str(project), offset=40)["scenarios"] == []
    assert tools.list_scenarios(str(project), offset=-1)["error_class"] == "invalid_argument"
    assert tools.list_scenarios(str(project), offset="3")["error_class"] == "invalid_argument"


def test_an_app_image_not_in_allowed_images_is_refused(monkeypatch, tmp_path) -> None:
    project = tmp_path / "deck"
    project.mkdir()
    for name in ("faultforge.yaml", "stack.yaml"):
        shutil.copy(DEMO / "sportsdeck" / name, project / name)
    configure(monkeypatch, tmp_path, [str(project)])  # allowed_images left empty, the default
    tools = Tools(run_stack=lambda *a: pytest.fail("an unlisted image must not run"), image_present=lambda image: pytest.fail("not inspected"))
    result = tools.run_scenario(str(project), "refresh-token-race", "image")
    assert result["error_class"] == "project_not_allowed" and str(tmp_path / "mcp.toml") in result["fix_hint"]
    assert SPORTSDECK_IMAGE in result["message"]
    validated = tools.validate_scenario(str(project))
    assert validated["valid"] is False and any("allowed_images" in error for error in validated["errors"])
    listed = tools.list_scenarios(str(project))
    assert listed["stack"]["app_image_allowed"] is False and listed["stack"]["app_image_present"] is None
    assert any("allowed_images" in problem for problem in listed["stack"]["problems"])
    # Another listed image doesn't cover this one.
    configure(monkeypatch, tmp_path, [str(project)], ["other:latest"])
    assert tools.run_scenario(str(project), "refresh-token-race", "image")["error_class"] == "project_not_allowed"
    assert Tools().status()["status"] == "ok"


def test_errors_are_structured_never_raised(monkeypatch, tmp_path, project) -> None:
    configure(monkeypatch, tmp_path, [str(project.parent), str(tmp_path)])

    def explode(*args, **kwargs):
        raise RuntimeError("a message that could quote a target")

    result = Tools(run_sandboxed=explode).run_scenario(str(project), SCENARIO, "worktree")
    assert result == {
        "status": "error", "error_class": "engine_failed", "message": "unexpected RuntimeError",
        "fix_hint": "This is a FaultForge bug; the run state is unchanged.",
    }
    tools = Tools(run_sandboxed=explode)
    assert tools.run_scenario(str(project), SCENARIO, "worktree", runtime="remote")["error_class"] == "runtime_unavailable"
    assert tools.run_scenario(str(project), SCENARIO, "worktree", runtime="cloud")["error_class"] == "invalid_argument"
    assert tools.run_scenario(str(project), "../x", "worktree")["error_class"] == "invalid_argument"
    assert tools.run_scenario(str(project), "nope", "worktree")["error_class"] == "config_invalid"
    assert tools.run_scenario(str(project), SCENARIO, "image")["error_class"] == "invalid_argument"
    plain = tmp_path / "plain"
    shutil.copytree(project, plain)
    assert tools.run_scenario(str(plain), SCENARIO, "worktree")["error_class"] == "not_a_git_repo"
    failed = Tools(run_sandboxed=lambda *a: SandboxResult(
        status="failed", failure_reason="image_missing", failure_detail="faultforge-target:dev isn't built",
        containment=ContainmentFacts(containers_left=0, wall_s=0.1, wall_limit_s=100.0),
    )).run_scenario(str(project), SCENARIO, "worktree")
    assert failed["error_class"] == "sandbox_failed" and "image_missing" in failed["message"] and failed["containers_left"] == 0


def test_a_stack_project_runs_only_its_prebuilt_image(monkeypatch, tmp_path) -> None:
    project = tmp_path / "deck"
    project.mkdir()
    for name in ("faultforge.yaml", "stack.yaml"):
        shutil.copy(DEMO / "sportsdeck" / name, project / name)
    configure(monkeypatch, tmp_path, [str(project)], [SPORTSDECK_IMAGE])
    calls = []

    def run_stack(spec, work_dir, scenario_id, options):
        calls.append(spec)
        return fake_result(work_dir)

    tools = Tools(run_stack=run_stack, run_sandboxed=lambda *a: pytest.fail("not a python target"))
    for source in ("worktree", "commit:1d9ea55"):
        result = tools.run_scenario(str(project), "refresh-token-race", source)
        assert result["error_class"] == "unsupported_build" and "M11" in result["fix_hint"]
    result = tools.run_scenario(str(project), "refresh-token-race", "image")
    assert result["status"] == "ok", result
    assert calls[0].app_image == SPORTSDECK_IMAGE
    assert result["source"]["rebuilt"] is False and any("not rebuilt" in note for note in result["notes"])
    listed = Tools(image_present=lambda image: image == SPORTSDECK_IMAGE).list_scenarios(str(project))
    assert listed["stack"]["app_image_allowed"] is True and listed["stack"]["app_image_present"] is True

    stack_text = (project / "stack.yaml").read_text(encoding="utf-8")
    for old, new in (
        ('app_user: "1001:1001"', 'app_user: "0:0"'),
        ("postgres_image: postgres:16-alpine", "postgres_image: evil:latest"),
        ("app_health_path: /api/tags", "app_health_path: '@evil/x'"),
        ("hooks_port: 8001", "hooks_port: 5432"),
        ("app_writable: [/app/.next/cache]", "app_writable: ['/app/x:exec']"),
        ("app_image: faultforge-sportsdeck:buggy", "app_image: --help"),
    ):
        (project / "stack.yaml").write_text(stack_text.replace(old, new), encoding="utf-8")
        assert tools.run_scenario(str(project), "refresh-token-race", "image")["error_class"] == "config_invalid"
        assert tools.validate_scenario(str(project))["valid"] is False
        if "--help" in new:
            inspected = []
            listed = Tools(image_present=inspected.append).list_scenarios(str(project))
            assert listed["stack"]["app_image_present"] is None and inspected == []
    assert len(calls) == 1
    (project / "stack.yaml").unlink()
    (project / "stack.yaml").symlink_to(DEMO / "sportsdeck" / "stack.yaml")
    assert tools.run_scenario(str(project), "refresh-token-race", "image")["error_class"] == "config_invalid"


def test_validate_reports_problems_without_running_anything(monkeypatch, tmp_path, project) -> None:
    configure(monkeypatch, tmp_path, [str(project.parent)])
    tools = Tools(run_sandboxed=lambda *a: pytest.fail("validate must not run"))
    assert tools.validate_scenario(str(project), SCENARIO)["valid"] is True
    assert tools.validate_scenario(str(project), "nope")["valid"] is False
    config = project / "faultforge.yaml"
    config.write_text(config.read_text(encoding="utf-8").replace("client.accepted <= before.stock", "__import__('os')", 1), encoding="utf-8")
    result = tools.validate_scenario(str(project))
    assert result["valid"] is False and result["errors"]


# The real server over stdio


def server_params(tmp_path: Path, roots: list[str], images: list[str] = ()) -> StdioServerParameters:
    config = tmp_path / "mcp.toml"
    config.write_text(f"allowed_roots = {json.dumps(roots)}\nallowed_images = {json.dumps(list(images))}\n", encoding="utf-8")
    return StdioServerParameters(
        command=sys.executable, args=["-m", "faultforge.mcp_server"],
        env={
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": os.environ.get("HOME", str(tmp_path)),
            settings_module.CONFIG_ENV: str(config), settings_module.STATE_ENV: str(tmp_path / "state"),
        },
    )


async def _call(session: ClientSession, name: str, arguments: dict | None = None, timeout_s: float = 30) -> dict:
    result = await asyncio.wait_for(session.call_tool(name, arguments or {}), timeout_s)
    assert not result.isError, result
    return result.structuredContent


def test_the_server_over_stdio(tmp_path) -> None:
    async def scenario() -> None:
        async with stdio_client(server_params(tmp_path, [str(DEMO)])) as (read, write):
            async with ClientSession(read, write) as session:
                await asyncio.wait_for(session.initialize(), 30)
                tools = await session.list_tools()
                assert {tool.name for tool in tools.tools} == TOOL_NAMES
                status = await _call(session, "faultforge_status")
                assert status["status"] == "ok" and status["allowed_roots"] == [str(DEMO)]
                assert status["credit"] is None and status["runtimes"]["remote"]["available"] is False
                listed = await _call(session, "faultforge_list_scenarios", {"project_dir": str(TICKET_API)})
                assert listed["status"] == "ok" and listed["scenarios"][0]["id"] == SCENARIO
                refused = await _call(session, "faultforge_list_scenarios", {"project_dir": str(ROOT)})
                assert refused["error_class"] == "project_not_allowed"

    asyncio.run(scenario())


@pytest.mark.docker
def test_the_ticket_api_fails_through_the_server(tmp_path) -> None:
    async def scenario() -> None:
        async with stdio_client(server_params(tmp_path, [str(DEMO)])) as (read, write):
            async with ClientSession(read, write) as session:
                await asyncio.wait_for(session.initialize(), 30)
                run = await _call(
                    session, "faultforge_run_scenario",
                    {"project_dir": str(TICKET_API), "scenario_id": SCENARIO, "source": "worktree"}, timeout_s=240,
                )
                assert run["status"] == "ok", run
                assert run["verdict"] == "FAIL" and run["containment"]["containers_left"] == 0
                assert run["containment"]["limits_applied"] is True
                assert any("No more tickets confirmed" in line for line in run["proven"])
                summary = await _call(session, "faultforge_get_run", {"run_id": run["run_id"]})
                assert summary["verdict"] == "FAIL"
                page = await _call(session, "faultforge_get_run", {"run_id": run["run_id"], "section": "evidence", "limit": 5})
                assert len(page["items"]) == 5 and page["total"] >= 20 and page["invalid_lines"] == 0
        assert list((tmp_path / "state" / "snapshots").iterdir()) == []

    asyncio.run(scenario())
