"""Builds a Node (pnpm) stack app from source into an image, with the network held to one step.

The steps, and what each one may do (docs/PLAN.md M11):

1. Lint, on the host. The lockfile, the workspace file and package.json must have a shape FaultForge
   knows: every package comes from the registry by sha512, nothing asks for install scripts, a
   pnpmfile, a patch or a workspace. Anything else is `unsupported_build`; nothing is ever given
   network as a fallback.
2. Fetch, on a cache miss only, and the one step with network. The container holds pnpm, the
   lockfile and a workspace file the host rewrote, and nothing else of the project. Its network is
   internal, with no gateway on the host, and reaches only FaultForge's proxy, which forwards to
   registry.npmjs.org:443 and nowhere else. No project code and no install script runs.
3. Link, on a cache miss only, with no network: the dev and prod node_modules are laid out in the
   cache volume from the store, with no install script.
4. The sealed build. This is where project code runs (a build imports the modules it bundles), so
   it gets no network, no secrets, a read-only root, hard limits and a non-root user, as
   stack_fix's build does. The dependency volume is mounted read-only, so the build can't change
   the cache; its own node_modules is a directory of links in memory.
5. The build's output and the prod node_modules are read as tar streams and rebuilt on the host by
   stack_fix's validator, then copied into an image by a Dockerfile with no RUN step.

The Node runtime entry a lockfile may carry (`devEngines.runtime`) is never fetched: Node comes
from the toolchain image. pnpm 12's `fetch` has no flag to skip it, so the fetch step gets a copy
of the lockfile without that entry, written again from what the lint parsed.

Nothing here prunes. A volume is removed only by a name computed here and only if it carries the
faultforge.depcache label; a network only by the exact name of the run that made it.
"""

import copy
import dataclasses
import hashlib
import json
import os
import re
import secrets
import shutil
import stat
import subprocess
import tarfile
import tempfile
import time
from collections.abc import Collection
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

import yaml
from pydantic import Field, field_validator, model_validator

from faultforge import config as config_module
from faultforge.errors import FaultForgeError
from faultforge.models import Contract
from faultforge.sandbox import (
    DEFAULT_POLICY,
    EXPIRES_LABEL,
    LABEL,
    ROLE_LABEL,
    SandboxError,
    SandboxPolicy,
    _clean,
    _created_at,
    _docker,
    _DockerTimeout,
    _mount,
    _remove_sandbox,
    code_dir_sha256,
)

FORMAT_VERSION = 1
PROXY_IMAGE = "faultforge-egress-proxy:dev"
REGISTRY = "https://registry.npmjs.org/"
DEPCACHE_LABEL = "faultforge.depcache"
# Which state directory a cache volume belongs to, so the sweep for orphans never touches another's.
DEPCACHE_STATE_LABEL = "faultforge.depcache.state"
CANDIDATE_LABEL = "faultforge.candidate"
BUILT_REPOSITORY = "faultforge-built"
CACHE_KEPT = 5
BUILD_USER = "1000:1000"
FETCH_TIMEOUT_S = 300.0
LINK_TIMEOUT_S = 120.0
IMAGE_TIMEOUT_S = 180.0
PROXY_READY_S = 15.0
INSTALL_MEMORY_MB = 2048
MAX_OUTPUT_BYTES = 256 * 1024 * 1024
MAX_PACKAGE_BYTES = 256 * 1024
MAX_LOCKFILE_BYTES = 8 * 1024 * 1024
MAX_WORKSPACE_BYTES = 64 * 1024
MAX_SOURCE_BYTES = 64 * 1024 * 1024
BUILD_LOG_LINES = 60
BUILD_LOG_CHARS = 2000
STALE_IMAGE_S = 2 * 3600
UNSUPPORTED = "unsupported_build"
NEEDS_SCRIPTS = "this project needs install scripts, which FaultForge never runs"
PROJECT = "[project]"

PACKAGE_NAME = "package.json"
LOCK_NAME = "pnpm-lock.yaml"
WORKSPACE_NAME = "pnpm-workspace.yaml"
NVMRC_NAME = ".nvmrc"
DEPENDENCY_FILES = (PACKAGE_NAME, LOCK_NAME, WORKSPACE_NAME, NVMRC_NAME)
# Never part of a fix: the files that decide what is installed, and the ones that define the test.
SCOPE_EXCLUDED = (*DEPENDENCY_FILES, ".npmrc", ".pnpmfile.cjs", ".pnpmfile.mjs", "stack.yaml", "faultforge.yaml", "BUSINESS_RULES.md", "node_modules")
REFUSED_AT_ROOT = ("node_modules", ".pnpmfile.cjs", ".pnpmfile.mjs", ".npmrc", "patches")
# The two of them that are directories: faultforge_validate_scenario copies files only, so it can't see them.
REFUSED_DIRECTORIES = ("node_modules", "patches")
REDUCED_PACKAGE_KEYS = (
    "name", "version", "private", "type", "packageManager", "engines", "devEngines",
    "dependencies", "devDependencies", "optionalDependencies",
)
DEPENDENCY_GROUPS = ("dependencies", "devDependencies", "optionalDependencies")
WORKSPACE_KEYS = {"allowBuilds", "engineStrict", "packages"}
WORKSPACE_SCRIPT_KEYS = {"onlyBuiltDependencies", "onlyBuiltDependenciesFile", "dangerouslyAllowAllBuilds", "neverBuiltDependencies"}
ENV_DOCUMENT_KEYS = {"lockfileVersion", "importers", "packages", "snapshots"}
MAIN_DOCUMENT_KEYS = {"lockfileVersion", "settings", "importers", "packages", "snapshots"}
SETTINGS_KEYS = {"autoInstallPeers", "excludeLinksFromLockfile"}
PACKAGE_ENTRY_KEYS = {
    "resolution", "engines", "cpu", "os", "libc", "hasBin", "deprecated", "peerDependencies", "peerDependenciesMeta",
    "bundledDependencies", "version",
}
SNAPSHOT_ENTRY_KEYS = {"dependencies", "optionalDependencies", "optional", "transitivePeerDependencies"}
LOCKFILE_VERSION = "9.0"
EXACT_VERSION = r"[0-9]{1,4}\.[0-9]{1,4}\.[0-9]{1,4}"
INTEGRITY = re.compile(r"sha512-[A-Za-z0-9+/]{86}==")
PACKAGE_KEY = re.compile(r"(@[a-z0-9][a-z0-9._~-]*/)?[a-z0-9][a-z0-9._~-]*@[0-9]+\.[0-9]+\.[0-9]+[0-9A-Za-z.+-]*")
PNPM_EXE = re.compile(r"@pnpm/exe(\.[a-z0-9-]+)?")
LOCAL_SPECIFIER = ("link:", "file:", "workspace:")
PLAIN_NAME = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,63}")
PLAIN_PATH = re.compile(r"[A-Za-z0-9_.-]{1,64}(/[A-Za-z0-9_.-]{1,64}){0,7}")
START_TOKEN = re.compile(r"[A-Za-z0-9_./=:@-]{1,128}")
SHOWN_KEY = re.compile(r"[^A-Za-z0-9@/._:-]")

POLICY = dataclasses.replace(DEFAULT_POLICY, docker_timeout_s=60.0)


class BuildConfig(Contract):
    """The `build:` section of stack.yaml. There is no command field: the build is the package's own
    `build` script, and the image names come from the versions, never from the project."""

    kind: Literal["node-pnpm"]
    node: str = Field(pattern=rf"^{EXACT_VERSION}$")
    pnpm: str = Field(pattern=rf"^{EXACT_VERSION}$")
    output: list[str] = Field(min_length=1, max_length=8)
    runtime_copy: list[str] = Field(default_factory=list, max_length=32)
    start: list[str] = Field(min_length=1, max_length=16)
    prod_dependencies: bool = False
    fix_scope: list[str] = Field(default_factory=lambda: ["src/"], min_length=1, max_length=16)

    @field_validator("output")
    @classmethod
    def output_is_plain_names(cls, value: list[str]) -> list[str]:
        if any(not PLAIN_NAME.fullmatch(name) or name in SCOPE_EXCLUDED or name.startswith(".") for name in value):
            raise ValueError("output entries must be plain directory names such as build")
        return value

    @field_validator("runtime_copy")
    @classmethod
    def runtime_copy_is_plain_paths(cls, value: list[str]) -> list[str]:
        if any(not PLAIN_PATH.fullmatch(path) or not _plain_parts(path) or path.split("/")[0] == "node_modules" for path in value):
            raise ValueError("runtime_copy entries must be plain relative paths such as package.json or migrations")
        return value

    @field_validator("start")
    @classmethod
    def start_is_a_plain_node_command(cls, value: list[str]) -> list[str]:
        if value[0] != "node" or any(not START_TOKEN.fullmatch(token) for token in value):
            raise ValueError("start must be an argument list that begins with node, such as [node, build]")
        return value

    @field_validator("fix_scope")
    @classmethod
    def fix_scope_is_allowed(cls, value: list[str]) -> list[str]:
        problems = scope_problems(value)
        if problems:
            raise ValueError(problems[0])
        return value

    @model_validator(mode="after")
    def output_and_runtime_copy_dont_collide(self) -> "BuildConfig":
        if {path.split("/")[0] for path in self.runtime_copy} & set(self.output):
            raise ValueError("runtime_copy can't name something the build outputs")
        if len(set(self.output)) != len(self.output):
            raise ValueError("output entries must be different")
        return self

    @property
    def build_image(self) -> str:
        return f"faultforge-node-build:{self.node}-pnpm{self.pnpm}"

    @property
    def run_image(self) -> str:
        return f"faultforge-node-run:{self.node}"


def _plain_parts(path: str) -> bool:
    return all(part not in ("", ".", "..") for part in path.split("/"))


# The fix scope


def scope_problems(scope: list[str]) -> list[str]:
    """Why a fix_scope can't be used. A scope entry is a directory (`src/`) or one file, and it can
    never reach a file that decides what is installed or what the test is."""
    problems = []
    for entry in scope:
        path = entry[:-1] if entry.endswith("/") else entry
        if not isinstance(entry, str) or not PLAIN_PATH.fullmatch(path) or not _plain_parts(path):
            problems.append("fix_scope entries must be plain relative paths such as src/")
            continue
        parts = path.split("/")
        if parts[0] in SCOPE_EXCLUDED or any(part == "node_modules" for part in parts) or parts[-1] in SCOPE_EXCLUDED:
            problems.append("fix_scope can't hold a dependency file, node_modules, stack.yaml, faultforge.yaml or BUSINESS_RULES.md")
    return problems


def in_scope(relative: str, scope: list[str]) -> bool:
    """Whether a candidate's file may replace the baseline's. A file that could steer an install is
    never in scope, wherever it sits."""
    parts = relative.split("/")
    if any(part == "node_modules" for part in parts) or parts[-1] in (".npmrc", ".pnpmfile.cjs", ".pnpmfile.mjs"):
        return False
    for entry in scope:
        if entry.endswith("/"):
            if relative.startswith(entry):
                return True
        elif relative == entry:
            return True
    return False


def _regular_files(root: Path) -> list[str]:
    """Relative posix paths of the regular files under root, sorted. Links and special files are skipped."""
    found = []
    for directory, subdirs, files in os.walk(root, followlinks=False):
        subdirs[:] = sorted(name for name in subdirs if not (Path(directory) / name).is_symlink())
        for name in files:
            path = Path(directory) / name
            if stat.S_ISREG(path.lstat().st_mode):
                found.append(path.relative_to(root).as_posix())
    return sorted(found)


def _copy_file(source: Path, destination: Path, mode: int = 0o644) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
    shutil.copyfile(source, destination, follow_symlinks=False)
    os.chmod(destination, mode)


def assemble_source(pinned: Path, candidate: Path | None, scope: list[str], destination: Path) -> None:
    """The source a build sees: the baseline's pinned tree with every path in the fix scope removed,
    plus the candidate's regular files in that scope. So a candidate changes nothing outside the
    scope, whatever else its tree edits. With no candidate it is the pinned tree."""
    destination.mkdir(parents=True, exist_ok=True, mode=0o755)
    for relative in _regular_files(pinned):
        if candidate is None or not in_scope(relative, scope):
            _copy_file(pinned / relative, destination / relative)
    if candidate is not None:
        for relative in _regular_files(candidate):
            if in_scope(relative, scope):
                _copy_file(candidate / relative, destination / relative)


def source_sha256(root: Path) -> str:
    """Of every file, in sorted path order: the path's length as 8 bytes, the path, the content's
    length as 8 bytes, the content. The lengths mean two different trees can't hash alike."""
    return code_dir_sha256(root)


# The lint


class Unsupported(Exception):
    """A project the builder won't build. The text is fixed, apart from a cleaned key name."""


@dataclass(frozen=True)
class Linted:
    lockfile: bytes            # the project's own bytes, for the offline link
    fetch_lockfile: bytes      # without the Node runtime entry, rewritten from what was parsed
    workspace: bytes           # rewritten: allowed keys only
    package: bytes             # reduced: no scripts
    packages: int


def _shown(key: object) -> str:
    return SHOWN_KEY.sub("?", str(key))[:40]


def _read_regular(root: Path, name: str, limit: int) -> bytes:
    path = root / name
    try:
        info = path.lstat()
    except OSError:
        raise Unsupported(f"{name} is missing") from None
    if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
        raise Unsupported(f"{name} must be a regular file under {limit} bytes")
    return path.read_bytes()


def _yaml_documents(data: bytes, name: str) -> list[object]:
    try:
        text = data.decode("utf-8")
        config_module._reject_hostile_yaml(text)
        return list(yaml.safe_load_all(text))
    except FaultForgeError:
        raise Unsupported(f"{name} uses YAML aliases or is nested too deep") from None
    except (UnicodeDecodeError, yaml.YAMLError, ValueError, TypeError, RecursionError):
        raise Unsupported(f"{name} isn't valid YAML") from None


def _mapping(value: object, what: str) -> dict:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise Unsupported(f"{what} must be a mapping")
    return value


def _only_keys(value: dict, allowed: set[str], what: str) -> None:
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise Unsupported(f"{what} has a key FaultForge doesn't know ({_shown(unknown[0])})")


def _lint_package(data: bytes, build: BuildConfig) -> tuple[dict, bool]:
    try:
        package = json.loads(data)
    except (ValueError, RecursionError):
        raise Unsupported(f"{PACKAGE_NAME} isn't valid JSON") from None
    package = _mapping(package, PACKAGE_NAME)
    if package.get("packageManager") != f"pnpm@{build.pnpm}":
        raise Unsupported(f"{PACKAGE_NAME}'s packageManager must be exactly pnpm@{build.pnpm}, the stack's build.pnpm")
    engines = package.get("engines")
    if engines is not None and _mapping(engines, "engines").get("node", build.node) != build.node:
        raise Unsupported(f"{PACKAGE_NAME}'s engines.node must be exactly {build.node}, the stack's build.node")
    if "pnpm" in package:
        raise Unsupported(f"{PACKAGE_NAME} has a pnpm field, which could change how dependencies install")
    if "workspaces" in package:
        raise Unsupported(f"{PACKAGE_NAME} declares workspaces, which FaultForge doesn't build")
    runtime = False
    if "devEngines" in package:
        engines = _mapping(package["devEngines"], "devEngines")
        entry = engines.get("runtime")
        if set(engines) != {"runtime"} or not isinstance(entry, dict) or not set(entry) <= {"name", "version", "onFail"}:
            raise Unsupported(f"{PACKAGE_NAME}'s devEngines may hold only runtime: {{name, version, onFail}}")
        if entry.get("name") != "node" or entry.get("version") != build.node or not isinstance(entry.get("onFail", ""), str):
            raise Unsupported(f"{PACKAGE_NAME}'s devEngines.runtime must be node {build.node}, the stack's build.node")
        runtime = True
    for group in DEPENDENCY_GROUPS:
        entries = _mapping(package.get(group, {}), group)
        for specifier in entries.values():
            if not isinstance(specifier, str) or specifier.startswith(LOCAL_SPECIFIER):
                raise Unsupported(f"{PACKAGE_NAME} has a dependency that isn't a registry version")
    scripts = package.get("scripts")
    if not isinstance(scripts, dict) or not isinstance(scripts.get("build"), str) or not scripts["build"].strip():
        raise Unsupported(f"{PACKAGE_NAME} has no build script")
    return package, runtime


def _lint_workspace(data: bytes) -> dict:
    documents = _yaml_documents(data, WORKSPACE_NAME)
    if len(documents) > 1:
        raise Unsupported(f"{WORKSPACE_NAME} must be one YAML document")
    workspace = _mapping(documents[0] if documents and documents[0] is not None else {}, WORKSPACE_NAME)
    if set(workspace) & WORKSPACE_SCRIPT_KEYS:
        raise Unsupported(NEEDS_SCRIPTS)
    unknown = sorted(set(workspace) - WORKSPACE_KEYS)
    if unknown:
        raise Unsupported(f"{WORKSPACE_NAME} has a setting FaultForge doesn't allow ({_shown(unknown[0])})")
    rewritten: dict[str, object] = {}
    if "allowBuilds" in workspace:
        builds = _mapping(workspace["allowBuilds"], "allowBuilds")
        if any(value is not False for value in builds.values()):
            raise Unsupported(NEEDS_SCRIPTS)
        rewritten["allowBuilds"] = {str(name): False for name in builds}
    if "engineStrict" in workspace:
        if not isinstance(workspace["engineStrict"], bool):
            raise Unsupported(f"{WORKSPACE_NAME}'s engineStrict must be true or false")
        rewritten["engineStrict"] = workspace["engineStrict"]
    if workspace.get("packages", ["."]) != ["."]:
        raise Unsupported(f"{WORKSPACE_NAME} declares workspace packages, which FaultForge doesn't build")
    return rewritten


def _integrity_only(entry: object) -> bool:
    return (
        isinstance(entry, dict) and isinstance(entry.get("resolution"), dict) and set(entry["resolution"]) == {"integrity"}
        and isinstance(entry["resolution"]["integrity"], str) and bool(INTEGRITY.fullmatch(entry["resolution"]["integrity"]))
    )


def _without_peers(reference: str) -> str:
    """pnpm appends the peers it resolved in parentheses: name@1.0.0(peer@2.0.0). The package is the part before."""
    return reference.split("(", 1)[0]


def _package_for(name: str, version: str) -> str:
    """The packages key a dependency resolves to. An alias's version already names the package
    (string-width-cjs: string-width@4.2.3); anything else is name@version."""
    version = _without_peers(version)
    return version if PACKAGE_KEY.fullmatch(version) else f"{name}@{version}"


def _require_linted(reference: str, packages: Collection[str]) -> None:
    """Every reference must land on a package the lint checked, so a URL, git or tarball source named
    only in a snapshot or an importer can never reach the fetch step."""
    if reference not in packages:
        raise Unsupported("the lockfile refers to a package that isn't a registry package with a sha512 integrity")


def _lint_snapshots(snapshots: object, what: str, packages: Collection[str]) -> None:
    for name, entry in _mapping(snapshots if snapshots is not None else {}, f"{what} snapshots").items():
        if not isinstance(name, str):
            raise Unsupported("a lockfile snapshot must be named by its package")
        _require_linted(_without_peers(name), packages)
        entry = _mapping(entry if entry is not None else {}, "a snapshot")
        _only_keys(entry, SNAPSHOT_ENTRY_KEYS, "a lockfile snapshot")
        for group in ("dependencies", "optionalDependencies"):
            for dependency, version in _mapping(entry.get(group, {}), group).items():
                if not isinstance(version, str) or version.startswith(LOCAL_SPECIFIER):
                    raise Unsupported("the lockfile has a link:, file: or workspace: dependency")
                _require_linted(_package_for(str(dependency), version), packages)


def _lint_env_document(document: dict, build: BuildConfig) -> None:
    _only_keys(document, ENV_DOCUMENT_KEYS, "the lockfile's first document")
    if document.get("lockfileVersion") != LOCKFILE_VERSION:
        raise Unsupported(f"the lockfile must be version {LOCKFILE_VERSION}")
    importers = _mapping(document.get("importers", {}), "importers")
    if set(importers) != {"."}:
        raise Unsupported("the lockfile must have exactly one importer, the project itself")
    importer = _mapping(importers["."], "the importer")
    _only_keys(importer, {"configDependencies", "packageManagerDependencies"}, "the lockfile's first importer")
    if importer.get("configDependencies"):
        raise Unsupported("the project uses config dependencies, which FaultForge doesn't install")
    managers = _mapping(importer.get("packageManagerDependencies", {}), "packageManagerDependencies")
    expected = {"specifier": build.pnpm, "version": build.pnpm}
    if set(managers) - {"pnpm"} or ("pnpm" in managers and managers["pnpm"] != expected):
        raise Unsupported(f"the lockfile's package manager must be pnpm {build.pnpm}, the stack's build.pnpm")
    for name, entry in _mapping(document.get("packages", {}), "packages").items():
        base, _, version = name.rpartition("@")
        if version != build.pnpm or not (base == "pnpm" or PNPM_EXE.fullmatch(base)) or not _integrity_only(entry):
            raise Unsupported("the lockfile's first document may hold only pnpm itself, by sha512")
        _only_keys(entry, PACKAGE_ENTRY_KEYS, "a lockfile package")
    _lint_snapshots(document.get("snapshots"), "the first document's", set(_mapping(document.get("packages", {}), "packages")))


def _lint_main_document(document: dict, build: BuildConfig, runtime_declared: bool) -> tuple[str | None, int]:
    """Returns the Node runtime entry's package name, if the lockfile has one, and the package count."""
    _only_keys(document, MAIN_DOCUMENT_KEYS, "the lockfile")
    if document.get("lockfileVersion") != LOCKFILE_VERSION:
        raise Unsupported(f"the lockfile must be version {LOCKFILE_VERSION}")
    settings = _mapping(document.get("settings", {}), "settings")
    _only_keys(settings, SETTINGS_KEYS, "the lockfile's settings")
    if settings.get("excludeLinksFromLockfile", False) is not False:
        raise Unsupported("the lockfile's excludeLinksFromLockfile must be false")
    if not isinstance(settings.get("autoInstallPeers", True), bool):
        raise Unsupported("the lockfile's autoInstallPeers must be true or false")
    importers = _mapping(document.get("importers", {}), "importers")
    if set(importers) != {"."}:
        raise Unsupported("the lockfile must have exactly one importer, the project itself")
    importer = _mapping(importers["."], "the importer")
    _only_keys(importer, set(DEPENDENCY_GROUPS), "the lockfile's importer")
    runtime_key = f"node@runtime:{build.node}"
    runtime_spec = f"runtime:{build.node}"
    referenced = False
    for group in DEPENDENCY_GROUPS:
        for name, entry in _mapping(importer.get(group, {}), group).items():
            if not isinstance(entry, dict) or set(entry) != {"specifier", "version"}:
                raise Unsupported("a lockfile importer entry must be a specifier and a version")
            specifier, version = entry["specifier"], entry["version"]
            if not isinstance(specifier, str) or not isinstance(version, str):
                raise Unsupported("a lockfile importer entry must be a specifier and a version")
            if specifier.startswith(LOCAL_SPECIFIER) or version.startswith(LOCAL_SPECIFIER):
                raise Unsupported("the lockfile has a link:, file: or workspace: dependency")
            if "runtime:" in specifier or "runtime:" in version:
                if name != "node" or specifier != runtime_spec or version != runtime_spec or referenced:
                    raise Unsupported(f"the lockfile's only runtime may be node {build.node}, the stack's build.node")
                referenced = True
    packages = _mapping(document.get("packages", {}), "packages")
    has_runtime = False
    for name, entry in packages.items():
        if "@runtime:" in name:
            if name != runtime_key or not referenced:
                raise Unsupported(f"the lockfile's only runtime may be node {build.node}, the stack's build.node")
            # Its resolution names nodejs.org archives. It is neither trusted nor used: this entry is never fetched.
            _only_keys(_mapping(entry, "the runtime entry"), PACKAGE_ENTRY_KEYS, "a lockfile package")
            has_runtime = True
            continue
        if not PACKAGE_KEY.fullmatch(name) or not _integrity_only(entry):
            raise Unsupported("every lockfile package must come from the registry, as name@version with a sha512 integrity and nothing else")
        _only_keys(entry, PACKAGE_ENTRY_KEYS, "a lockfile package")
    if referenced != has_runtime or (has_runtime and not runtime_declared) or (runtime_declared and not has_runtime):
        raise Unsupported(f"the Node runtime entry must appear in {PACKAGE_NAME}'s devEngines, the importer and the packages together")
    for group in DEPENDENCY_GROUPS:
        for name, entry in _mapping(importer.get(group, {}), group).items():
            if name == "node" and entry["version"] == runtime_spec:
                continue
            _require_linted(_package_for(name, entry["version"]), packages)
    _lint_snapshots(document.get("snapshots"), "the", packages)
    return (runtime_key if has_runtime else None), len(packages)


def _without_runtime(documents: list[dict], runtime_key: str | None) -> bytes:
    """The lockfile the fetch step sees: no Node runtime entry in the importer, the packages or the
    snapshots. Written again from the parsed data, so it holds nothing the lint didn't read."""
    documents = copy.deepcopy(documents)
    if runtime_key is not None:
        main = documents[-1]
        for group in DEPENDENCY_GROUPS:
            main["importers"]["."].get(group, {}).pop("node", None)
        main.get("packages", {}).pop(runtime_key, None)
        (main.get("snapshots") or {}).pop(runtime_key, None)
    return yaml.safe_dump_all(documents, sort_keys=False, explicit_start=True, width=1_000_000).encode("utf-8")


def lint(root: Path, build: BuildConfig) -> Linted:
    """Raises Unsupported with one fixed sentence. Reads only; runs nothing."""
    for name in REFUSED_AT_ROOT:
        if os.path.lexists(root / name):
            raise Unsupported(NEEDS_SCRIPTS if name.startswith(".pnpmfile") else f"the source has a {name} at its root, which FaultForge doesn't build with")
    package, runtime_declared = _lint_package(_read_regular(root, PACKAGE_NAME, MAX_PACKAGE_BYTES), build)
    if os.path.lexists(root / NVMRC_NAME):
        nvmrc = _read_regular(root, NVMRC_NAME, 64).decode("ascii", "replace").strip().removeprefix("v")
        if nvmrc != build.node:
            raise Unsupported(f"{NVMRC_NAME} must be exactly {build.node}, the stack's build.node")
    workspace = _lint_workspace(_read_regular(root, WORKSPACE_NAME, MAX_WORKSPACE_BYTES))
    lockfile = _read_regular(root, LOCK_NAME, MAX_LOCKFILE_BYTES)
    documents = _yaml_documents(lockfile, LOCK_NAME)
    if not 1 <= len(documents) <= 2:
        raise Unsupported("the lockfile must be one or two YAML documents")
    documents = [_mapping(document, "a lockfile document") for document in documents]
    if len(documents) == 2:
        _lint_env_document(documents[0], build)
    runtime_key, count = _lint_main_document(documents[-1], build, runtime_declared)
    reduced = {key: package[key] for key in REDUCED_PACKAGE_KEYS if key in package}
    return Linted(
        lockfile=lockfile,
        fetch_lockfile=_without_runtime(documents, runtime_key),
        workspace=yaml.safe_dump(workspace, sort_keys=True).encode("utf-8"),
        package=(json.dumps(reduced, indent=2, sort_keys=True) + "\n").encode("utf-8"),
        packages=count,
    )


def lint_problems(root: Path, build: BuildConfig) -> list[str]:
    try:
        lint(root, build)
    except Unsupported as error:
        return [str(error)]
    return []


# The install cache


def cache_key(build_image_id: str, linted: Linted, state: Path) -> str:
    """Changes with the toolchain, the lockfile, the rewritten workspace file and the reduced
    package.json: source edits don't touch it. The state directory is part of it too, because a
    volume's index entry lives there: two state directories on one Docker daemon then never share a
    volume, so neither can take the other's for half-written and remove it."""
    digest = hashlib.sha256()
    home = str(Path(state).resolve()).encode("utf-8", "surrogateescape")
    for part in (build_image_id.encode(), linted.lockfile, linted.workspace, linted.package, str(FORMAT_VERSION).encode(), home):
        digest.update(len(part).to_bytes(8, "big") + part)
    return digest.hexdigest()


def state_tag(state: Path) -> str:
    return hashlib.sha256(str(Path(state).resolve()).encode("utf-8")).hexdigest()[:16]


def volume_name(key: str) -> str:
    return f"ff-deps-{key[:16]}"


def cache_dir(state: Path) -> Path:
    path = state / "depcache"
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    return path


def _index_path(state: Path, key: str) -> Path:
    if not re.fullmatch(r"[a-f0-9]{64}", key):
        raise ValueError("not a cache key")
    return cache_dir(state) / f"{key}.json"


def _volume_labels(policy: SandboxPolicy, name: str) -> dict | None:
    """The volume's labels, or None if there is no such volume."""
    result = _docker(policy, ["volume", "inspect", "--format", "{{json .Labels}}", name])
    if result.returncode != 0:
        return None
    try:
        labels = json.loads(result.stdout)
    except ValueError:
        labels = None
    return labels if isinstance(labels, dict) else {}


def remove_cache_volume(policy: SandboxPolicy, name: str) -> bool:
    """By the exact name, and only a volume that carries the cache label. Never a prune."""
    labels = _volume_labels(policy, name)
    if labels is None:
        return True
    if not re.fullmatch(r"ff-deps-[a-f0-9]{16}", name) or DEPCACHE_LABEL not in labels:
        return False
    return _docker(policy, ["volume", "rm", name]).returncode == 0


def cache_ready(state: Path, key: str, policy: SandboxPolicy = POLICY) -> bool:
    """True only for a volume whose index entry was written, which happens last. A volume without an
    entry is half-written and is removed by name; an entry without a volume is dropped."""
    index, name = _index_path(state, key), volume_name(key)
    exists = _volume_labels(policy, name) is not None
    if exists and index.is_file():
        return True
    if exists:
        remove_cache_volume(policy, name)
    index.unlink(missing_ok=True)
    return False


def _write_index(state: Path, key: str, facts: dict[str, object]) -> None:
    path = _index_path(state, key)
    temp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temp.write_text(json.dumps({"key": key, "volume": volume_name(key), **facts}, indent=2) + "\n", encoding="utf-8")
    os.replace(temp, path)


def touch_cache(state: Path, key: str) -> None:
    os.utime(_index_path(state, key), None)


def cache_keys(state: Path) -> list[str]:
    """The keys held, most recently used first. Reads only: it never makes the directory."""
    directory = state / "depcache"
    if not directory.is_dir():
        return []
    entries = [path for path in directory.glob("*.json") if re.fullmatch(r"[a-f0-9]{64}", path.stem)]
    return [path.stem for path in sorted(entries, key=lambda path: path.stat().st_mtime, reverse=True)]


def evict(state: Path, policy: SandboxPolicy = POLICY, keep: int = CACHE_KEPT) -> list[str]:
    """Drops all but the most recently used keys, each volume by its exact name. Then removes this
    state directory's own volumes that have no index entry: a fetch that was killed for a lockfile
    that never comes back. Only volumes carrying this state directory's tag are listed; never a prune."""
    removed = []
    for key in cache_keys(state)[keep:]:
        if remove_cache_volume(policy, volume_name(key)):
            _index_path(state, key).unlink(missing_ok=True)
            removed.append(key)
    indexed = {volume_name(key) for key in cache_keys(state)}
    listed = _docker(policy, ["volume", "ls", "--quiet", "--filter", f"label={DEPCACHE_STATE_LABEL}={state_tag(state)}"])
    if listed.returncode == 0:
        for name in listed.stdout.decode("ascii", "replace").split():
            if name not in indexed and remove_cache_volume(policy, name):
                removed.append(name)
    return removed


# Container commands


def _labels(sandbox_id: str, role: str, expires_at: int) -> list[str]:
    return ["--label", f"{LABEL}={sandbox_id}", "--label", f"{ROLE_LABEL}={role}", "--label", f"{EXPIRES_LABEL}={expires_at}"]


def _sealed(name: str, sandbox_id: str, role: str, expires_at: int, memory_mb: int, cpus: float, pids: int, log_mb: int) -> list[str]:
    memory = f"{memory_mb}m"
    return [
        "run", "--detach", "--pull", "never", "--name", name, *_labels(sandbox_id, role, expires_at),
        "--read-only", "--cap-drop", "ALL", "--security-opt", "no-new-privileges", "--ipc", "none",
        "--memory", memory, "--memory-swap", memory, "--cpus", f"{cpus:g}", "--pids-limit", str(pids),
        "--log-driver", "json-file", "--log-opt", f"max-size={log_mb}m", "--log-opt", "max-file=1",
    ]


def _tmpfs(path: str, size_mb: int) -> list[str]:
    uid, gid = BUILD_USER.split(":")
    return ["--tmpfs", f"{path}:rw,nosuid,nodev,uid={uid},gid={gid},size={size_mb}m"]


def install_network(sandbox_id: str) -> str:
    return f"ff-{sandbox_id}-install"


def egress_network(sandbox_id: str) -> str:
    return f"ff-{sandbox_id}-egress"


def proxy_name(sandbox_id: str) -> str:
    return f"ff-{sandbox_id}-proxy"


def network_argv(sandbox_id: str, expires_at: int) -> dict[str, list[str]]:
    """Both networks belong to this run. The install network is internal and has no gateway address
    on the host, so its containers reach each other and nothing else, not even the host."""
    labels = ["--label", f"{LABEL}={sandbox_id}", "--label", f"{EXPIRES_LABEL}={expires_at}"]
    return {
        "install": [
            "network", "create", "--internal", "--ipv6=false", "--opt", "com.docker.network.bridge.gateway_mode_ipv4=isolated",
            *labels, install_network(sandbox_id),
        ],
        "egress": ["network", "create", "--ipv6=false", *labels, egress_network(sandbox_id)],
    }


def proxy_argv(sandbox_id: str, expires_at: int) -> list[str]:
    """Starts on the install network alone and listens on its address there. The egress network is
    connected afterwards, so the listener is never bound on the outward leg."""
    return [
        *_sealed(proxy_name(sandbox_id), sandbox_id, "proxy", expires_at, memory_mb=128, cpus=0.5, pids=256, log_mb=1),
        "--network", install_network(sandbox_id), "--user", "65534:65534",
        PROXY_IMAGE, "python", "-I", "/opt/egress_proxy.py", "--listen", "self:3128",
    ]


FETCH_SCRIPT = (
    "mkdir -p /deps/dev /deps/prod && cp /in/pnpm-lock.yaml /in/pnpm-workspace.yaml /deps/dev/ && cd /deps/dev && "
    f"pnpm fetch --ignore-pnpmfile --store-dir /deps/store --registry {REGISTRY}"
)
_INSTALL = "pnpm install --frozen-lockfile --offline --ignore-scripts --ignore-pnpmfile --no-runtime --trust-lockfile"
LINK_SCRIPT = (
    "for d in dev prod; do cp /in/pnpm-lock.yaml /in/pnpm-workspace.yaml /in/package.json /deps/$d/ || exit 1; done && "
    f"cd /deps/dev && {_INSTALL} --store-dir /deps/store && "
    f"cd /deps/prod && {_INSTALL} --prod --store-dir /deps/store --config.package-import-method=copy"
)


def fetch_argv(name: str, sandbox_id: str, image: str, inputs: Path, volume: str, expires_at: int) -> list[str]:
    """The one networked step. It sees the lockfile and the rewritten workspace file, and nothing
    else of the project: no package.json, no source, no stack settings."""
    proxy = f"http://{proxy_name(sandbox_id)}:3128"
    return [
        *_sealed(name, sandbox_id, "install", expires_at, INSTALL_MEMORY_MB, 2, 512, 4),
        "--network", install_network(sandbox_id), "--user", BUILD_USER, *_tmpfs("/tmp", 256),
        "--mount", _mount(inputs, "/in", read_only=True),
        "--mount", f"type=volume,src={volume},dst=/deps",
        "--env", "HOME=/tmp", "--env", "CI=1", "--env", f"HTTPS_PROXY={proxy}", "--env", f"HTTP_PROXY={proxy}",
        "--entrypoint", "sh", image, "-c", FETCH_SCRIPT,
    ]


def link_argv(name: str, sandbox_id: str, image: str, inputs: Path, volume: str, expires_at: int) -> list[str]:
    """No network. Both installs skip the Node runtime and every install script. --trust-lockfile
    skips pnpm's supply-chain policy check, which needs registry metadata; the fetch step ran it."""
    return [
        *_sealed(name, sandbox_id, "install", expires_at, INSTALL_MEMORY_MB, 2, 512, 4),
        "--network", "none", "--user", BUILD_USER, *_tmpfs("/tmp", 256),
        "--mount", _mount(inputs, "/in", read_only=True),
        "--mount", f"type=volume,src={volume},dst=/deps",
        "--env", "HOME=/tmp", "--env", "CI=1",
        "--entrypoint", "sh", image, "-c", LINK_SCRIPT,
    ]


def _stack_fix():
    # Imported late: stack_fix imports stack, which imports BuildConfig from this module.
    from faultforge import stack_fix

    return stack_fix


def build_argv(name: str, sandbox_id: str, image: str, source: Path, volume: str, expires_at: int) -> list[str]:
    """Where project code runs. The dependency volume is read-only; /app/node_modules is a directory
    of links to it, in memory, because SvelteKit and Vite write generated files there. pnpm's
    install-before-run check is off: with no network and a read-only store it could only fail."""
    fix = _stack_fix()
    script = (
        "cp -R /src/. /app/ && cd /app && mkdir node_modules && "
        "ln -s /deps/node_modules/* /deps/node_modules/.[!.]* node_modules/ && "
        f"pnpm --offline --ignore-pnpmfile run build; echo $? > /tmp/build-exit; sleep {int(fix.COPY_TIMEOUT_S) * 2}"
    )
    return [
        *_sealed(name, sandbox_id, "build", expires_at, fix.BUILD_MEMORY_MB, fix.BUILD_CPUS, fix.BUILD_PIDS, 4),
        "--network", "none", "--user", BUILD_USER, *_tmpfs("/tmp", 512), *_tmpfs("/app", fix.BUILD_TMPFS_MB),
        "--mount", _mount(source, "/src", read_only=True),
        "--mount", f"type=volume,src={volume},dst=/deps/node_modules,volume-subpath=dev/node_modules,readonly",
        "--env", "HOME=/tmp", "--env", "CI=1", "--env", "NODE_ENV=production",
        "--env", f"NODE_OPTIONS=--max-old-space-size={fix.BUILD_HEAP_MB}",
        "--env", "PNPM_CONFIG_VERIFY_DEPS_BEFORE_RUN=false",
        "--entrypoint", "sh", image, "-c", script,
    ]


def deps_argv(name: str, sandbox_id: str, image: str, volume: str, expires_at: int) -> list[str]:
    """A trusted container that only holds the prod node_modules for the host to read as a tar."""
    fix = _stack_fix()
    return [
        *_sealed(name, sandbox_id, "deps", expires_at, 256, 1, 64, 1),
        "--network", "none", "--user", BUILD_USER,
        "--mount", f"type=volume,src={volume},dst=/deps/node_modules,volume-subpath=prod/node_modules,readonly",
        "--entrypoint", "sleep", image, str(int(fix.COPY_TIMEOUT_S) * 2),
    ]


def dockerfile(build: BuildConfig) -> str:
    """No RUN step: the image is the toolchain's Node plus files the host validated."""
    return (
        f"FROM {build.run_image}\n"
        "WORKDIR /app\n"
        "COPY --chown=0:0 app/ /app/\n"
        "ENV NODE_ENV=production\n"
        f"USER {BUILD_USER}\n"
        f"CMD {json.dumps(build.start)}\n"
    )


def image_argv(staging: Path, tag: str, candidate: bool) -> list[str]:
    label = ["--label", f"{CANDIDATE_LABEL}=1"] if candidate else []
    return ["build", "--quiet", "--network", "none", "--pull=false", *label, "--tag", tag, str(staging)]


# The pipeline


@dataclass(frozen=True)
class StackBuild:
    """What a build made, or why it didn't. failure_detail is fixed text; log_tail is the build's own
    output, so project text, cleaned and cut."""

    image_id: str | None = None
    image: str | None = None
    source_sha256: str | None = None
    failure_reason: str | None = None
    failure_detail: str | None = None
    log_tail: str | None = None
    install: str | None = None
    proxy: tuple[str, ...] = ()
    seconds: dict[str, float] = field(default_factory=dict)
    cache_key: str | None = None
    toolchain: dict[str, str] = field(default_factory=dict)
    build_peak_mb: int | None = None

    def line(self) -> str:
        """One line of facts for a result: fixed words and numbers only."""
        parts = [f"install {self.install}" if self.install else "install not reached"]
        parts += [f"{name} {value:.1f} s" for name, value in self.seconds.items()]
        if self.build_peak_mb is not None:
            parts.append(f"build peak {self.build_peak_mb} MiB")
        return ", ".join(parts)


def _image_id(policy: SandboxPolicy, image: str) -> str | None:
    result = _docker(policy, ["image", "inspect", "--format", "{{.Id}}", "--", image])
    return result.stdout.decode("ascii", "replace").strip() if result.returncode == 0 else None


def toolchain_ids(build: BuildConfig, policy: SandboxPolicy = POLICY) -> dict[str, str | None]:
    return {"build": _image_id(policy, build.build_image), "run": _image_id(policy, build.run_image)}


def toolchain_images(policy: SandboxPolicy = POLICY) -> list[str]:
    """The toolchain images on this machine, by name. Raises SandboxError if docker can't be used."""
    found = []
    for repository in ("faultforge-node-build", "faultforge-node-run"):
        listed = _docker(policy, ["image", "ls", "--filter", f"reference={repository}", "--format", "{{.Repository}}:{{.Tag}}"])
        found += [line for line in listed.stdout.decode("ascii", "replace").split() if re.fullmatch(r"[a-z-]+:[A-Za-z0-9.]+", line)]
    return sorted(found)


def toolchain_hint(build: BuildConfig) -> str:
    return f"Build the toolchain images: bash scripts/build_node_images.sh {build.node} {build.pnpm}, and scripts/build_images.sh for the proxy."


def _wait(policy: SandboxPolicy, name: str, limit_s: float) -> int | None:
    """The container's exit code, or None if it outlived its limit and was killed."""
    try:
        waited = _docker(policy, ["wait", name], limit_s)
    except _DockerTimeout:
        _docker(policy, ["kill", name])
        return None
    try:
        return int(waited.stdout.strip())
    except ValueError:
        return None


def _log_tail(policy: SandboxPolicy, name: str) -> str:
    logs = _docker(policy, ["logs", "--tail", str(BUILD_LOG_LINES), name], 30.0, stderr=subprocess.STDOUT)
    text = re.sub(r"\x1b\[[0-9;]*m", "", logs.stdout.decode("utf-8", "replace"))
    return _clean(text, BUILD_LOG_CHARS)


def proxy_summary(log: bytes) -> tuple[str, ...]:
    """The proxy's own lines, counted. A refused host is shown cleaned; the proxy already cut it."""
    allowed, received, refused = 0, 0, {}
    for line in log.splitlines()[:100_000]:
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if not isinstance(entry, dict) or "allowed" not in entry:
            continue
        if entry["allowed"] is True:
            allowed += 1
            received += int(entry.get("bytes_received") or 0)
        else:
            what = f"{_shown(entry.get('host', ''))}:{_shown(entry.get('port', ''))}" if "host" in entry else _shown(entry.get("reason"))
            refused[what] = refused.get(what, 0) + 1
    lines = [f"registry.npmjs.org:443: {allowed} connections, {received} bytes received"]
    lines += [f"refused {what}: {count}" for what, count in sorted(refused.items())[:8]]
    return tuple(lines)


def _write_inputs(directory: Path, files: dict[str, bytes]) -> Path:
    directory.mkdir(mode=0o755)
    for name, data in files.items():
        (directory / name).write_bytes(data)
        os.chmod(directory / name, 0o644)
    return directory


def _fetch(policy: SandboxPolicy, sandbox_id: str, image: str, linted: Linted, volume: str, work: Path, expires_at: int) -> tuple[bool, str, tuple[str, ...]]:
    """(ok, log tail, proxy lines). The networks and both containers are removed on every path."""
    inputs = _write_inputs(work / "fetch-in", {LOCK_NAME: linted.fetch_lockfile, WORKSPACE_NAME: linted.workspace})
    networks = network_argv(sandbox_id, expires_at)
    name = f"ff-{sandbox_id}-install"
    proxy = proxy_name(sandbox_id)
    lines: tuple[str, ...] = ()
    try:
        for argv in networks.values():
            if _docker(policy, argv).returncode != 0:
                return False, "the install networks couldn't be created", lines
        if _docker(policy, proxy_argv(sandbox_id, expires_at)).returncode != 0:
            return False, "the proxy couldn't start", lines
        deadline = time.monotonic() + PROXY_READY_S
        while b'"listening"' not in _docker(policy, ["logs", proxy]).stdout:
            if time.monotonic() > deadline:
                return False, "the proxy didn't start listening", lines
            time.sleep(0.25)
        if _docker(policy, ["network", "connect", egress_network(sandbox_id), proxy]).returncode != 0:
            return False, "the proxy couldn't join its egress network", lines
        if _docker(policy, fetch_argv(name, sandbox_id, image, inputs, volume, expires_at)).returncode != 0:
            return False, "the fetch container couldn't start", lines
        status = _wait(policy, name, FETCH_TIMEOUT_S)
        lines = proxy_summary(_docker(policy, ["logs", proxy]).stdout)
        if status != 0:
            return False, _log_tail(policy, name) if status is not None else f"the fetch passed its {FETCH_TIMEOUT_S:g} s limit", lines
        return True, "", lines
    finally:
        try:
            _remove_sandbox(policy, sandbox_id)
        except SandboxError:
            pass
        for network in (install_network(sandbox_id), egress_network(sandbox_id)):
            try:
                _docker(policy, ["network", "rm", network])
            except SandboxError:
                pass


def _link(policy: SandboxPolicy, sandbox_id: str, image: str, linted: Linted, volume: str, work: Path, expires_at: int) -> tuple[bool, str]:
    inputs = _write_inputs(work / "link-in", {LOCK_NAME: linted.lockfile, WORKSPACE_NAME: linted.workspace, PACKAGE_NAME: linted.package})
    name = f"ff-{sandbox_id}-link"
    try:
        if _docker(policy, link_argv(name, sandbox_id, image, inputs, volume, expires_at)).returncode != 0:
            return False, "the link container couldn't start"
        status = _wait(policy, name, LINK_TIMEOUT_S)
        if status != 0:
            return False, _log_tail(policy, name) if status is not None else f"the link passed its {LINK_TIMEOUT_S:g} s limit"
        return True, ""
    finally:
        try:
            _remove_sandbox(policy, sandbox_id)
        except SandboxError:
            pass


def _copy_runtime(source: Path, app: Path, paths: list[str]) -> bool:
    """Regular files only, from the source the build used. False if a path isn't there."""
    for relative in paths:
        origin = source / relative
        if origin.is_symlink():
            return False
        if origin.is_file():
            _copy_file(origin, app / relative)
        elif origin.is_dir():
            for name in _regular_files(origin):
                _copy_file(origin / name, app / relative / name)
        else:
            return False
    return True


def _peak_mb(policy: SandboxPolicy, name: str) -> int | None:
    """From the build container's cgroup, read through the container: a number or nothing."""
    try:
        result = _docker(policy, ["exec", name, "cat", "/sys/fs/cgroup/memory.peak"], 15.0)
        return int(result.stdout.strip()) // (1024 * 1024) if result.returncode == 0 else None
    except (SandboxError, ValueError):
        return None


def build_stack_image(
    source: Path, build: BuildConfig, state: Path, *, allow_fetch: bool, tag: str | None = None, policy: SandboxPolicy = POLICY,
) -> StackBuild:
    """Never raises for a bad project: every failure is a StackBuild with a reason.

    allow_fetch is the caller's permission for the one networked step, used only on a cache miss.
    With tag, the image keeps that name and isn't marked as a throwaway candidate (the owner's demo
    builds); without, it gets a random faultforge-built name and the caller removes it."""
    seconds: dict[str, float] = {}
    started = time.monotonic()
    sweep_images(policy)
    ids = toolchain_ids(build, policy)
    proxy_needed = allow_fetch and _image_id(policy, PROXY_IMAGE) is None
    if ids["build"] is None or ids["run"] is None:
        return StackBuild(failure_reason="image_missing", failure_detail=f"{build.build_image} or {build.run_image} isn't built. {toolchain_hint(build)}")
    toolchain = {"build": ids["build"], "run": ids["run"]}
    sandbox_id = f"sb-{secrets.token_hex(6)}"
    fix = _stack_fix()
    expires_at = int(time.time() + FETCH_TIMEOUT_S + LINK_TIMEOUT_S + fix.BUILD_TIMEOUT_S + 3 * fix.COPY_TIMEOUT_S + IMAGE_TIMEOUT_S + policy.expiry_margin_s)
    with tempfile.TemporaryDirectory(prefix="faultforge-nodebuild-") as temp:
        work = Path(temp)
        work.chmod(0o755)
        tree = work / "src"
        total = 0
        for relative in _regular_files(source):
            total += (source / relative).stat().st_size
            if total > MAX_SOURCE_BYTES:
                return StackBuild(failure_reason=UNSUPPORTED, failure_detail="the source is too large to build", toolchain=toolchain)
            _copy_file(source / relative, tree / relative)
        tree.mkdir(exist_ok=True)

        def failed(reason: str, detail: str | None = None, **extra: object) -> StackBuild:
            return StackBuild(failure_reason=reason, failure_detail=detail, seconds=dict(seconds), toolchain=toolchain, **extra)  # type: ignore[arg-type]

        try:
            linted = lint(tree, build)
        except Unsupported as error:
            return failed(UNSUPPORTED, str(error))
        digest = source_sha256(tree)
        key = cache_key(ids["build"], linted, state)
        volume = volume_name(key)
        seconds["lint"] = time.monotonic() - started
        install, proxy_lines = "cache hit", ()
        try:
            if cache_ready(state, key, policy):
                touch_cache(state, key)
            else:
                if not allow_fetch:
                    return failed("install_cache_missing", "the dependencies for this lockfile aren't in the install cache", cache_key=key, source_sha256=digest)
                if proxy_needed:
                    return failed("image_missing", f"{PROXY_IMAGE} isn't built. {toolchain_hint(build)}", cache_key=key)
                if _volume_labels(policy, volume) is not None:
                    # cache_ready found it half-written but couldn't remove it: never fetch over it.
                    return failed("docker_unavailable", "a half-written install cache couldn't be removed", cache_key=key)
                create = [
                    "volume", "create", "--label", f"{DEPCACHE_LABEL}={key[:16]}",
                    "--label", f"{DEPCACHE_STATE_LABEL}={state_tag(state)}", volume,
                ]
                if _docker(policy, create).returncode != 0:
                    return failed("docker_unavailable", "the cache volume couldn't be created", cache_key=key)
                step = time.monotonic()
                ok, tail, proxy_lines = _fetch(policy, sandbox_id, ids["build"], linted, volume, work, expires_at)
                seconds["fetch"] = time.monotonic() - step
                if ok:
                    step = time.monotonic()
                    ok, tail = _link(policy, sandbox_id, ids["build"], linted, volume, work, expires_at)
                    seconds["link"] = time.monotonic() - step
                    stage = "link"
                else:
                    stage = "fetch"
                if not ok:
                    remove_cache_volume(policy, volume)
                    return failed(
                        UNSUPPORTED, f"the dependency {stage} failed. If a package needs an install script: {NEEDS_SCRIPTS}.",
                        log_tail=tail, proxy=proxy_lines, cache_key=key, install=f"{stage} failed",
                    )
                # Last, so a volume with an entry is always a complete one.
                _write_index(state, key, {"toolchain": ids["build"], "packages": linted.packages, "proxy": list(proxy_lines), "created": int(time.time())})
                install = "fetched"
                evict(state, policy)

            def done(reason: str, detail: str | None = None, **extra: object) -> StackBuild:
                return failed(reason, detail, install=install, proxy=proxy_lines, cache_key=key, source_sha256=digest, **extra)

            name = f"ff-{sandbox_id}-build"
            step = time.monotonic()
            if _docker(policy, build_argv(name, sandbox_id, ids["build"], tree, volume, expires_at)).returncode != 0:
                return done("build_not_started")
            status = fix._wait_for_build(policy, name)
            seconds["build"] = time.monotonic() - step
            peak = _peak_mb(policy, name)
            if status is None:
                return done("build_timed_out", build_peak_mb=peak)
            if status != 0:
                return done(
                    "build_failed", f"the build script failed. If a package needs an install script: {NEEDS_SCRIPTS}.",
                    log_tail=_log_tail(policy, name), build_peak_mb=peak,
                )
            step = time.monotonic()
            staging = work / "staging"
            app = staging / "app"
            app.mkdir(parents=True, mode=0o755)
            try:
                fix._copy_output(policy, name, app, "/app", tuple(build.output), MAX_OUTPUT_BYTES)
                if build.prod_dependencies:
                    deps = f"ff-{sandbox_id}-deps"
                    if _docker(policy, deps_argv(deps, sandbox_id, ids["build"], volume, expires_at)).returncode != 0:
                        return done("build_output_missing", "the prod dependencies couldn't be read", build_peak_mb=peak)
                    fix._copy_output(policy, deps, app, "/deps", ("node_modules",), MAX_OUTPUT_BYTES)
            except (fix.UnsafeOutput, tarfile.TarError, OSError, EOFError) as error:
                reason = str(error) if isinstance(error, fix.UnsafeOutput) else type(error).__name__
                return done("build_output_rejected", f"The build's output was refused: {reason}.", build_peak_mb=peak)
            if not all((app / item).is_dir() and not (app / item).is_symlink() for item in build.output):
                return done("build_output_missing", "the build didn't produce every directory in build.output", build_peak_mb=peak)
            if build.prod_dependencies and not (app / "node_modules").is_dir():
                return done("build_output_missing", "the prod dependencies couldn't be read", build_peak_mb=peak)
            if not _copy_runtime(tree, app, build.runtime_copy):
                return done(UNSUPPORTED, "build.runtime_copy names a path the source doesn't have", build_peak_mb=peak)
            seconds["copy"] = time.monotonic() - step
            step = time.monotonic()
            (staging / "Dockerfile").write_text(dockerfile(build), encoding="utf-8")
            image = tag or f"{BUILT_REPOSITORY}:{digest[:12]}-{secrets.token_hex(4)}"
            built = _docker(policy, image_argv(staging, image, candidate=tag is None), IMAGE_TIMEOUT_S)
            seconds["image"] = time.monotonic() - step
            if built.returncode != 0:
                return done("candidate_image_failed", build_peak_mb=peak)
            return StackBuild(
                image_id=built.stdout.decode("ascii", "replace").strip(), image=image, source_sha256=digest, install=install,
                proxy=proxy_lines, seconds=dict(seconds), cache_key=key, toolchain=toolchain, build_peak_mb=peak,
            )
        except SandboxError as error:
            return failed(f"build_{error.reason}")
        finally:
            try:
                _remove_sandbox(policy, sandbox_id)
            except SandboxError:
                pass  # The reaper removes it by its expiry label.


def remove_images(images: list[str], policy: SandboxPolicy = POLICY) -> None:
    """By the ID or name a build returned."""
    for image in images:
        try:
            _docker(policy, ["image", "rm", "--force", image], 60.0)
        except SandboxError:
            pass


def sweep_images(policy: SandboxPolicy = POLICY, max_age_s: float = STALE_IMAGE_S, now: float | None = None) -> int:
    """Removes built images a killed server left: only faultforge-built ones that carry the candidate
    label and are older than max_age_s, each by its ID."""
    try:
        listed = _docker(policy, [
            "image", "ls", "--quiet", "--no-trunc", "--filter", f"reference={BUILT_REPOSITORY}", "--filter", f"label={CANDIDATE_LABEL}=1",
        ])
        ids = sorted({item for item in listed.stdout.decode("ascii", "replace").split() if re.fullmatch(r"sha256:[a-f0-9]{64}", item)})
        if not ids:
            return 0
        described = _docker(policy, ["image", "inspect", "--format", "{{.Id}} {{.Created}}", *ids])
    except SandboxError:
        return 0
    now = time.time() if now is None else now
    stale = []
    for line in described.stdout.decode("utf-8", "replace").splitlines():
        identifier, _, created = line.partition(" ")
        made = _created_at(created)
        if identifier in ids and made is not None and now - made.timestamp() >= max_age_s:
            stale.append(identifier)
    remove_images(stale, policy)
    return len(stale)
