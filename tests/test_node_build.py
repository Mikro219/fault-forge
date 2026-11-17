"""M11: the Node builder without Docker. The lint, the fix scope and its overlay, the commands each
step runs, the install cache's recovery and the registry-only proxy (on loopback, with a fake
resolver and a fake upstream)."""

import copy
import io
import json
import os
import shutil
import socket
import subprocess
import tarfile
import threading
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from faultforge import egress_proxy, node_build, sandbox
from faultforge.node_build import BuildConfig, Unsupported
from faultforge.stack import load_stack
from faultforge.stack_fix import UnsafeOutput, extract_output

ROOT = Path(__file__).resolve().parents[1]
DEMO = ROOT / "demo" / "redeem-code"
BUILD = load_stack(DEMO / "stack.yaml").build
DEPENDENCY_FILES = ("package.json", "pnpm-lock.yaml", "pnpm-workspace.yaml", ".nvmrc")
SHA = "sha512-" + "A" * 86 + "=="

# The shape of a real SvelteKit project's lockfile: two documents, settings, and a Node runtime entry
# whose resolution is a set of nodejs.org archives.
JADE_SHAPED_LOCK = f"""---
lockfileVersion: '9.0'

importers:

  .:
    configDependencies: {{}}
    packageManagerDependencies:
      pnpm:
        specifier: 12.21.0
        version: 12.21.0

packages:

  '@pnpm/exe.linux-x64-musl@12.21.0':
    resolution: {{integrity: {SHA}}}
    cpu: [x64]
    os: [linux]
    libc: [musl]

  pnpm@12.21.0:
    resolution: {{integrity: {SHA}}}
    engines: {{node: '>=18'}}
    hasBin: true

snapshots:

  '@pnpm/exe.linux-x64-musl@12.21.0':
    optional: true

  pnpm@12.21.0:
    optionalDependencies:
      '@pnpm/exe.linux-x64-musl': 12.21.0

---
lockfileVersion: '9.0'

settings:
  autoInstallPeers: true
  excludeLinksFromLockfile: false

importers:

  .:
    dependencies:
      pg:
        specifier: 8.23.1
        version: 8.23.1
    devDependencies:
      node:
        specifier: runtime:24.22.3
        version: runtime:24.22.3

packages:

  node@runtime:24.22.3:
    resolution:
      type: variations
      variants:
        - resolution:
            archive: tarball
            bin:
              node: bin/node
            integrity: sha256-aT/ojEqF5+Fy7BMvdg5ns5JbLWRCyxyzp0tz/Pgx9wo=
            type: binary
            url: https://nodejs.org/download/release/v24.22.3/node-v24.22.3-linux-x64.tar.gz
          targets:
            - cpu: x64
              os: linux
    version: 24.22.3
    hasBin: true

  pg@8.23.1:
    resolution: {{integrity: {SHA}}}
    engines: {{node: '>= 16.0.0'}}

snapshots:

  node@runtime:24.22.3: {{}}

  pg@8.23.1: {{}}
"""
JADE_SHAPED_PACKAGE = {
    "name": "shaped", "private": True, "type": "module", "packageManager": "pnpm@12.21.0",
    "engines": {"node": "24.22.3", "pnpm": ">=12.21.0 <13"},
    "devEngines": {"runtime": {"name": "node", "version": "24.22.3", "onFail": "download"}},
    "scripts": {"build": "vite build"}, "dependencies": {"pg": "8.23.1"},
}


@pytest.fixture
def project(tmp_path: Path) -> Path:
    root = tmp_path / "project"
    root.mkdir()
    for name in DEPENDENCY_FILES:
        shutil.copy(DEMO / name, root / name)
    return root


def lock_documents(root: Path) -> list[dict]:
    return list(yaml.safe_load_all((root / "pnpm-lock.yaml").read_text(encoding="utf-8")))


def write_lock(root: Path, documents: list[dict]) -> None:
    (root / "pnpm-lock.yaml").write_text(yaml.safe_dump_all(documents, sort_keys=False, explicit_start=True), encoding="utf-8")


def edit_package(root: Path, **changes: object) -> None:
    package = json.loads((root / "package.json").read_text(encoding="utf-8"))
    package.update(changes)
    (root / "package.json").write_text(json.dumps(package), encoding="utf-8")


# The lint


def test_the_demo_passes_the_lint_and_has_the_real_shape(project: Path) -> None:
    linted = node_build.lint(project, BUILD)
    documents = lock_documents(project)
    assert len(documents) == 2 and set(documents[1]["settings"]) == {"autoInstallPeers", "excludeLinksFromLockfile"}
    assert "node@runtime:24.22.3" in documents[1]["packages"] and documents[1]["packages"]["node@runtime:24.22.3"]["resolution"]["type"] == "variations"
    assert documents[1]["importers"]["."]["devDependencies"]["node"] == {"specifier": "runtime:24.22.3", "version": "runtime:24.22.3"}
    assert linted.lockfile == (project / "pnpm-lock.yaml").read_bytes() and linted.packages == len(documents[1]["packages"])


def test_a_hand_written_lockfile_of_the_real_shape_passes(tmp_path: Path) -> None:
    (tmp_path / "pnpm-lock.yaml").write_text(JADE_SHAPED_LOCK, encoding="utf-8")
    (tmp_path / "package.json").write_text(json.dumps(JADE_SHAPED_PACKAGE), encoding="utf-8")
    (tmp_path / "pnpm-workspace.yaml").write_text("allowBuilds:\n  esbuild: false\nengineStrict: true\n", encoding="utf-8")
    (tmp_path / ".nvmrc").write_text("24.22.3\n", encoding="utf-8")
    linted = node_build.lint(tmp_path, BUILD)
    assert yaml.safe_load(linted.workspace) == {"allowBuilds": {"esbuild": False}, "engineStrict": True}
    assert linted.packages == 2


def test_the_fetch_step_gets_a_lockfile_without_the_node_runtime(project: Path) -> None:
    """In the importer, the packages and the snapshots: pnpm fetch has no flag to skip it."""
    linted = node_build.lint(project, BUILD)
    assert b"node@runtime" in linted.lockfile and b"nodejs.org" in linted.lockfile
    assert b"runtime:" not in linted.fetch_lockfile and b"nodejs.org" not in linted.fetch_lockfile
    real, fetched = lock_documents(project), list(yaml.safe_load_all(linted.fetch_lockfile))
    assert fetched[0] == real[0]
    for section in ("packages", "snapshots"):
        assert set(real[1][section]) - set(fetched[1][section]) == {"node@runtime:24.22.3"}
    assert "node" not in fetched[1]["importers"]["."]["devDependencies"]
    del real[1]["importers"]["."]["devDependencies"]["node"]
    assert fetched[1]["importers"] == real[1]["importers"]


def test_the_reduced_package_keeps_dev_engines_and_drops_scripts(project: Path) -> None:
    edit_package(project, somethingElse={"x": 1})
    reduced = json.loads(node_build.lint(project, BUILD).package)
    assert "scripts" not in reduced and "pnpm" not in reduced and "somethingElse" not in reduced and "imports" not in reduced
    assert reduced["devEngines"] == {"runtime": {"name": "node", "version": "24.22.3", "onFail": "download"}}
    assert reduced["packageManager"] == "pnpm@12.21.0" and reduced["dependencies"] == {"pg": "8.23.1"}


def main_document(mutate):
    def change(root: Path) -> None:
        documents = lock_documents(root)
        mutate(documents[1])
        write_lock(root, documents)
    return change


def env_document(mutate):
    def change(root: Path) -> None:
        documents = lock_documents(root)
        mutate(documents[0])
        write_lock(root, documents)
    return change


def first_package(document: dict) -> dict:
    return next(entry for name, entry in document["packages"].items() if "@runtime:" not in name)


def rename_runtime(document: dict, name: str, specifier: str) -> None:
    document["packages"][name] = document["packages"].pop("node@runtime:24.22.3")
    document["snapshots"][name] = document["snapshots"].pop("node@runtime:24.22.3")
    document["importers"]["."]["devDependencies"]["node"] = {"specifier": specifier, "version": specifier}


def add_second_runtime(document: dict) -> None:
    document["packages"]["bun@runtime:1.2.0"] = copy.deepcopy(document["packages"]["node@runtime:24.22.3"])
    document["importers"]["."]["devDependencies"]["bun"] = {"specifier": "runtime:1.2.0", "version": "runtime:1.2.0"}


def deno_runtime(document: dict) -> None:
    document["packages"]["deno@runtime:2.0.0"] = document["packages"].pop("node@runtime:24.22.3")
    del document["importers"]["."]["devDependencies"]["node"]
    document["importers"]["."]["devDependencies"]["deno"] = {"specifier": "runtime:2.0.0", "version": "runtime:2.0.0"}


def write(name: str, text: str):
    return lambda root: (root / name).write_text(text, encoding="utf-8")


REFUSED = {
    "three documents": lambda root: write_lock(root, [*lock_documents(root), {"lockfileVersion": "9.0"}]),
    "an unknown top-level key in the first document": env_document(lambda document: document.update(overrides={})),
    "an unknown top-level key in the main document": main_document(lambda document: document.update(patchedDependencies={})),
    "an unknown settings key": main_document(lambda document: document["settings"].update(injectWorkspacePackages=True)),
    "excludeLinksFromLockfile true": main_document(lambda document: document["settings"].update(excludeLinksFromLockfile=True)),
    "a runtime entry for another node": main_document(lambda document: rename_runtime(document, "node@runtime:22.1.0", "runtime:22.1.0")),
    "a deno runtime": main_document(deno_runtime),
    "two runtime entries": main_document(add_second_runtime),
    "a runtime package no importer names": main_document(lambda document: document["importers"]["."]["devDependencies"].pop("node")),
    "a variations resolution on an ordinary package": main_document(
        lambda document: first_package(document).update(resolution={"type": "variations", "variants": []})),
    "a url resolution": main_document(
        lambda document: first_package(document)["resolution"].update(url="https://example.invalid/x.tgz")),
    "a tarball resolution": main_document(
        lambda document: first_package(document).update(resolution={"tarball": "https://example.invalid/x.tgz", "integrity": SHA})),
    "a git resolution": main_document(
        lambda document: first_package(document).update(resolution={"type": "git", "repo": "https://example.invalid/x.git", "commit": "a" * 40})),
    "a sha1 integrity": main_document(lambda document: first_package(document).update(resolution={"integrity": "sha1-" + "A" * 27 + "="})),
    "a package keyed by a url": main_document(
        lambda document: document["packages"].update({"x@https://example.invalid/x.tgz": {"resolution": {"integrity": SHA}}})),
    "a link dependency": main_document(
        lambda document: document["importers"]["."]["dependencies"].update(local={"specifier": "link:../x", "version": "link:../x"})),
    "an unknown package key": main_document(lambda document: first_package(document).update(patched=True)),
    "a first-document package that isn't pnpm": env_document(
        lambda document: document["packages"].update({"left-pad@12.21.0": {"resolution": {"integrity": SHA}}})),
    "another pnpm version in the first document": env_document(
        lambda document: document["importers"]["."]["packageManagerDependencies"]["pnpm"].update(version="12.0.0")),
    "config dependencies": env_document(lambda document: document["importers"]["."].update(configDependencies={"x": "1.0.0"})),
    "a second importer": main_document(lambda document: document["importers"].update({"packages/x": {}})),
    "another lockfile version": main_document(lambda document: document.update(lockfileVersion="6.0")),
    "devEngines for another node": lambda root: edit_package(root, devEngines={"runtime": {"name": "node", "version": "22.1.0", "onFail": "download"}}),
    "devEngines with a package manager": lambda root: edit_package(
        root, devEngines={"runtime": {"name": "node", "version": "24.22.3"}, "packageManager": {"name": "pnpm"}}),
    "no devEngines beside a runtime entry": lambda root: edit_package(root, devEngines=None) or edit_package(root, **{}),
    "allowBuilds true": write("pnpm-workspace.yaml", "allowBuilds:\n  esbuild: true\n"),
    "onlyBuiltDependencies": write("pnpm-workspace.yaml", "onlyBuiltDependencies: [esbuild]\n"),
    "dangerouslyAllowAllBuilds": write("pnpm-workspace.yaml", "dangerouslyAllowAllBuilds: true\n"),
    "an unknown workspace key": write("pnpm-workspace.yaml", "shamefullyHoist: true\n"),
    "a registry in the workspace file": write("pnpm-workspace.yaml", "registry: https://example.invalid/\n"),
    "workspace packages": write("pnpm-workspace.yaml", "packages: ['apps/*']\n"),
    "a pnpmfile": write(".pnpmfile.cjs", "module.exports = {}\n"),
    "an npmrc": write(".npmrc", "registry=https://example.invalid/\n"),
    "a tracked node_modules": lambda root: (root / "node_modules").mkdir(),
    "a patches directory": lambda root: (root / "patches").mkdir(),
    "another packageManager": lambda root: edit_package(root, packageManager="pnpm@12.21.1"),
    "a packageManager with a hash": lambda root: edit_package(root, packageManager="pnpm@12.21.0+sha512.abc"),
    "another engines.node": lambda root: edit_package(root, engines={"node": ">=24"}),
    "another nvmrc": write(".nvmrc", "22.1.0\n"),
    "a pnpm field in package.json": lambda root: edit_package(root, pnpm={"overrides": {}}),
    "workspaces in package.json": lambda root: edit_package(root, workspaces=["apps/*"]),
    "no build script": lambda root: edit_package(root, scripts={"dev": "vite dev"}),
    "a file dependency in package.json": lambda root: edit_package(root, dependencies={"pg": "file:../pg"}),
    "an oversized lockfile": lambda root: (root / "pnpm-lock.yaml").write_bytes(b"#" * (node_build.MAX_LOCKFILE_BYTES + 1)),
    "a lockfile that isn't YAML": write("pnpm-lock.yaml", "{ not: [yaml"),
    "a lockfile with an alias": write("pnpm-lock.yaml", "a: &x {b: 1}\nc: *x\n"),
    "no workspace file": lambda root: (root / "pnpm-workspace.yaml").unlink(),
    # Every reference must land on a checked registry package, so a source named only here never reaches the fetch.
    "a snapshot no checked package backs": main_document(lambda d: d["snapshots"].__setitem__("evil@https://evil.example/x.tgz", {})),
    "a second runtime, as a snapshot only": main_document(lambda d: d["snapshots"].__setitem__("node@runtime:99.0.0", {})),
    "a snapshot dependency on a git repo": main_document(lambda d: d["snapshots"].__setitem__(
        next(name for name in d["snapshots"] if "@runtime:" not in name),
        {"dependencies": {"other": "git+ssh://git@evil.example/r.git#abc"}},
    )),
    "an importer version that is a URL": main_document(lambda d: d["importers"]["."]["dependencies"].__setitem__(
        "evil", {"specifier": "https://evil.example/x.tgz", "version": "https://evil.example/x.tgz"},
    )),
}


@pytest.mark.parametrize("name", list(REFUSED))
def test_the_lint_refuses(project: Path, name: str) -> None:
    if name == "no devEngines beside a runtime entry":
        package = json.loads((project / "package.json").read_text(encoding="utf-8"))
        del package["devEngines"]
        (project / "package.json").write_text(json.dumps(package), encoding="utf-8")
    else:
        REFUSED[name](project)
    with pytest.raises(Unsupported) as refused:
        node_build.lint(project, BUILD)
    message = str(refused.value)
    assert message and "\n" not in message and len(message) < 200
    assert node_build.lint_problems(project, BUILD) == [message]


def test_a_symlinked_lockfile_is_refused(project: Path, tmp_path: Path) -> None:
    elsewhere = tmp_path / "elsewhere.yaml"
    shutil.move(project / "pnpm-lock.yaml", elsewhere)
    (project / "pnpm-lock.yaml").symlink_to(elsewhere)
    with pytest.raises(Unsupported, match="regular file"):
        node_build.lint(project, BUILD)


def test_a_project_that_asks_for_install_scripts_is_told_so(project: Path) -> None:
    for text in ("allowBuilds:\n  esbuild: true\n", "onlyBuiltDependencies: [esbuild]\n"):
        (project / "pnpm-workspace.yaml").write_text(text, encoding="utf-8")
        assert node_build.lint_problems(project, BUILD) == [node_build.NEEDS_SCRIPTS]


def test_a_project_without_a_runtime_entry_passes_with_one_document(project: Path) -> None:
    documents = lock_documents(project)
    main = documents[1]
    del main["packages"]["node@runtime:24.22.3"], main["snapshots"]["node@runtime:24.22.3"], main["importers"]["."]["devDependencies"]["node"]
    write_lock(project, [main])
    package = json.loads((project / "package.json").read_text(encoding="utf-8"))
    del package["devEngines"]
    (project / "package.json").write_text(json.dumps(package), encoding="utf-8")
    linted = node_build.lint(project, BUILD)
    assert list(yaml.safe_load_all(linted.fetch_lockfile)) == [main]


# The build section and the fix scope


def build_config(**changes: object) -> BuildConfig:
    return BuildConfig.model_validate({**BUILD.model_dump(), **changes})


@pytest.mark.parametrize("changes", [
    {"node": "24"}, {"node": "24.x"}, {"pnpm": "latest"}, {"kind": "npm"}, {"output": ["../build"]}, {"output": ["node_modules"]},
    {"output": ["build", "build"]}, {"output": []}, {"runtime_copy": ["/etc/passwd"]}, {"runtime_copy": ["a/../b"]},
    {"runtime_copy": ["node_modules/pg"]}, {"runtime_copy": ["build/x"]}, {"start": ["sh", "-c", "x"]}, {"start": ["node", "a b"]},
    {"start": ["node", "$(id)"]}, {"start": []}, {"command": "make"}, {"image": "evil:latest"},
    {"fix_scope": ["package.json"]}, {"fix_scope": ["pnpm-lock.yaml"]}, {"fix_scope": ["pnpm-workspace.yaml"]}, {"fix_scope": [".nvmrc"]},
    {"fix_scope": [".npmrc"]}, {"fix_scope": ["stack.yaml"]}, {"fix_scope": ["faultforge.yaml"]}, {"fix_scope": ["BUSINESS_RULES.md"]},
    {"fix_scope": ["node_modules/"]}, {"fix_scope": ["src/node_modules/"]}, {"fix_scope": ["src/.pnpmfile.cjs"]}, {"fix_scope": ["./"]},
    {"fix_scope": [""]}, {"fix_scope": ["/"]}, {"fix_scope": ["../other/"]}, {"fix_scope": ["/abs/"]}, {"fix_scope": []},
])
def test_a_build_section_that_isnt_plain_is_refused(changes: dict) -> None:
    with pytest.raises(ValidationError):
        build_config(**changes)


def test_the_build_section_derives_its_images_and_takes_a_wider_scope() -> None:
    assert BUILD.build_image == "faultforge-node-build:24.22.3-pnpm12.21.0" and BUILD.run_image == "faultforge-node-run:24.22.3"
    assert BUILD.fix_scope == ["src/", "migrations/"]
    wider = build_config(fix_scope=["src/", "migrations/", "vite.config.ts"])
    assert node_build.in_scope("migrations/0001.sql", wider.fix_scope) and node_build.in_scope("vite.config.ts", wider.fix_scope)
    assert not node_build.in_scope("vite.config.ts.bak", wider.fix_scope) and not node_build.in_scope("srcx/a.ts", wider.fix_scope)
    for name in ("src/node_modules/x/index.js", "src/.npmrc", "src/lib/.pnpmfile.cjs"):
        assert not node_build.in_scope(name, wider.fix_scope)
    assert "command" not in BuildConfig.model_fields


def tree(root: Path, files: dict[str, str]) -> Path:
    for name, text in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return root


def contents(root: Path) -> dict[str, str]:
    return {name: (root / name).read_text(encoding="utf-8") for name in node_build._regular_files(root)}


def test_only_the_fix_scope_comes_from_the_candidate(tmp_path: Path) -> None:
    pinned = tree(tmp_path / "pinned", {
        "package.json": "base", "stack.yaml": "base", "faultforge.yaml": "base", "migrations/0.sql": "base",
        "src/a.ts": "base", "src/gone.ts": "base", "src/lib/b.ts": "base",
    })
    candidate = tree(tmp_path / "candidate", {
        "package.json": "edited", "stack.yaml": "edited", "faultforge.yaml": "edited", "migrations/0.sql": "edited", "extra.txt": "new",
        "src/a.ts": "fixed", "src/lib/b.ts": "base", "src/new.ts": "added", "src/node_modules/evil/index.js": "x", "src/.npmrc": "x",
    })
    (candidate / "src" / "link.ts").symlink_to(candidate / "package.json")
    built = tmp_path / "built"
    node_build.assemble_source(pinned, candidate, ["src/"], built)
    assert contents(built) == {
        "package.json": "base", "stack.yaml": "base", "faultforge.yaml": "base", "migrations/0.sql": "base",
        "src/a.ts": "fixed", "src/lib/b.ts": "base", "src/new.ts": "added",
    }
    same = tmp_path / "same"
    node_build.assemble_source(pinned, pinned, ["src/"], same)
    plain = tmp_path / "plain"
    node_build.assemble_source(pinned, None, ["src/"], plain)
    assert node_build.source_sha256(same) == node_build.source_sha256(plain) == node_build.source_sha256(pinned)
    assert node_build.source_sha256(built) != node_build.source_sha256(pinned)
    # Edits outside the scope alone leave the hash where it was.
    outside = tmp_path / "outside"
    shutil.copytree(pinned, tmp_path / "edited")
    tree(tmp_path / "edited", {"package.json": "edited", "stack.yaml": "edited", "pnpm-lock.yaml": "new"})
    node_build.assemble_source(pinned, tmp_path / "edited", ["src/"], outside)
    assert node_build.source_sha256(outside) == node_build.source_sha256(pinned)


def test_the_source_hash_keeps_names_and_contents_apart(tmp_path: Path) -> None:
    first = tree(tmp_path / "first", {"a": "bc"})
    second = tree(tmp_path / "second", {"ab": "c"})
    third = tree(tmp_path / "third", {"a": "b", "c": ""})
    assert len({node_build.source_sha256(root) for root in (first, second, third)}) == 3
    # Stable, and the order files were written in doesn't matter.
    forwards = tree(tmp_path / "forwards", {"x/1": "one", "x/2": "two", "y": "three"})
    backwards = tree(tmp_path / "backwards", {"y": "three", "x/2": "two", "x/1": "one"})
    assert node_build.source_sha256(forwards) == node_build.source_sha256(backwards) == node_build.source_sha256(forwards)


# The commands


def flags(argv: list[str], name: str) -> list[str]:
    return [argv[index + 1] for index, item in enumerate(argv) if item == name]


def assert_sealed(argv: list[str], user: str = "1000:1000") -> None:
    assert argv[:2] == ["run", "--detach"] and flags(argv, "--pull") == ["never"]
    assert "--read-only" in argv and flags(argv, "--cap-drop") == ["ALL"] and flags(argv, "--security-opt") == ["no-new-privileges"]
    assert flags(argv, "--ipc") == ["none"] and flags(argv, "--user") == [user]
    assert flags(argv, "--memory") == flags(argv, "--memory-swap") and flags(argv, "--pids-limit") and flags(argv, "--cpus")
    assert "--privileged" not in argv and "--cap-add" not in argv and not any("docker.sock" in item for item in argv)
    labels = flags(argv, "--label")
    assert any(label.startswith("faultforge.sandbox=") for label in labels) and any(label.startswith("faultforge.expires=") for label in labels)
    for item in argv:
        assert not any(word in item.upper() for word in ("VULTR", "API_KEY", "TOKEN", "SECRET", "PASSWORD", "DATABASE_URL"))


def test_the_fetch_step_sees_only_the_lockfile_and_reaches_only_the_proxy(tmp_path: Path) -> None:
    argv = node_build.fetch_argv("ff-sb-x-install", "sb-x", "sha256:build", tmp_path / "in", "ff-deps-0123456789abcdef", 99)
    assert_sealed(argv)
    assert flags(argv, "--network") == ["ff-sb-x-install"]
    mounts = flags(argv, "--mount")
    assert mounts == [f"type=bind,src={tmp_path / 'in'},dst=/in,readonly", "type=volume,src=ff-deps-0123456789abcdef,dst=/deps"]
    env = dict(item.split("=", 1) for item in flags(argv, "--env"))
    assert env == {"HOME": "/tmp", "CI": "1", "HTTPS_PROXY": "http://ff-sb-x-proxy:3128", "HTTP_PROXY": "http://ff-sb-x-proxy:3128"}
    script = argv[-1]
    assert "pnpm fetch --ignore-pnpmfile" in script and "--registry https://registry.npmjs.org/" in script
    assert "package.json" not in script and "install" not in script and "run " not in script
    assert argv[argv.index("--entrypoint") + 1] == "sh" and argv[-3] == "sha256:build"


def test_the_networks_belong_to_the_run_and_the_proxy_never_joins_the_default_bridge() -> None:
    networks = node_build.network_argv("sb-x", 99)
    install, egress = networks["install"], networks["egress"]
    assert install[:3] == ["network", "create", "--internal"] and install[-1] == "ff-sb-x-install"
    assert "com.docker.network.bridge.gateway_mode_ipv4=isolated" in install
    assert egress[-1] == "ff-sb-x-egress" and "--internal" not in egress
    for argv in (install, egress):
        assert "faultforge.sandbox=sb-x" in argv and "faultforge.expires=99" in argv
    proxy = node_build.proxy_argv("sb-x", 99)
    assert_sealed(proxy, "65534:65534")
    assert flags(proxy, "--network") == ["ff-sb-x-install"] and "bridge" not in proxy and "host" not in proxy
    assert proxy[-5:] == ["python", "-I", "/opt/egress_proxy.py", "--listen", "self:3128"]
    assert not flags(proxy, "--mount") and not flags(proxy, "--env") and not any("0.0.0.0" in item for item in proxy)


def test_the_link_and_the_build_have_no_network(tmp_path: Path) -> None:
    link = node_build.link_argv("ff-sb-x-link", "sb-x", "sha256:build", tmp_path / "in", "ff-deps-0123456789abcdef", 99)
    assert_sealed(link)
    assert flags(link, "--network") == ["none"] and not any("PROXY" in item for item in link)
    installs = [part for part in link[-1].split("&&") if "pnpm install" in part]
    assert len(installs) == 2 and "--prod" in installs[1] and "--prod" not in installs[0]
    for install in installs:
        for flag in ("--frozen-lockfile", "--offline", "--ignore-scripts", "--ignore-pnpmfile", "--no-runtime"):
            assert flag in install
    assert "--config.package-import-method=copy" in installs[1]

    build = node_build.build_argv("ff-sb-x-build", "sb-x", "sha256:build", tmp_path / "src", "ff-deps-0123456789abcdef", 99)
    assert_sealed(build)
    assert flags(build, "--network") == ["none"] and flags(build, "--memory") == ["3072m"] and flags(build, "--pids-limit") == ["1024"]
    assert flags(build, "--mount") == [
        f"type=bind,src={tmp_path / 'src'},dst=/src,readonly",
        "type=volume,src=ff-deps-0123456789abcdef,dst=/deps/node_modules,volume-subpath=dev/node_modules,readonly",
    ]
    assert "pnpm --offline --ignore-pnpmfile run build" in build[-1] and "install" not in build[-1].replace("--offline", "")
    env = dict(item.split("=", 1) for item in flags(build, "--env"))
    assert set(env) == {"HOME", "CI", "NODE_ENV", "NODE_OPTIONS", "PNPM_CONFIG_VERIFY_DEPS_BEFORE_RUN"}

    deps = node_build.deps_argv("ff-sb-x-deps", "sb-x", "sha256:build", "ff-deps-0123456789abcdef", 99)
    assert_sealed(deps)
    assert flags(deps, "--network") == ["none"] and all(mount.endswith(",readonly") for mount in flags(deps, "--mount"))


def test_the_image_has_no_run_step_and_starts_as_the_build_section_says() -> None:
    text = node_build.dockerfile(BUILD)
    assert text.splitlines() == [
        "FROM faultforge-node-run:24.22.3", "WORKDIR /app", "COPY --chown=0:0 app/ /app/", "ENV NODE_ENV=production",
        "USER 1000:1000", 'CMD ["node", "build"]',
    ]
    assert "RUN" not in text and "ADD" not in text
    argv = node_build.image_argv(Path("/staging"), "faultforge-built:abc-1", candidate=True)
    assert argv[:5] == ["build", "--quiet", "--network", "none", "--pull=false"] and "faultforge.candidate=1" in argv
    assert "faultforge.candidate=1" not in node_build.image_argv(Path("/staging"), "faultforge-redeem-code:buggy", candidate=False)


def test_nothing_ever_prunes() -> None:
    tmp_path = Path("/work")
    every = [
        *node_build.network_argv("sb-x", 1).values(), node_build.proxy_argv("sb-x", 1),
        node_build.fetch_argv("n", "sb-x", "i", tmp_path, "v", 1), node_build.link_argv("n", "sb-x", "i", tmp_path, "v", 1),
        node_build.build_argv("n", "sb-x", "i", tmp_path, "v", 1), node_build.deps_argv("n", "sb-x", "i", "v", 1),
        node_build.image_argv(tmp_path, "t", True),
    ]
    assert not any("prune" in item for argv in every for item in argv)
    for module in ("node_build.py", "stack.py", "sandbox.py", "stack_fix.py", "mcp_server/tools.py", "mcp_server/store.py"):
        code = [line for line in (ROOT / "src" / "faultforge" / module).read_text(encoding="utf-8").splitlines() if '"prune"' in line or "'prune'" in line]
        assert code == []


# The install cache


class FakeDocker:
    """Volumes only: enough to see which docker commands the cache would run."""

    def __init__(self, volumes: dict[str, dict], stuck: tuple[str, ...] = ()):
        self.volumes = volumes
        self.stuck = stuck
        self.calls: list[list[str]] = []

    def __call__(self, policy, args, timeout_s=None, **_):
        self.calls.append(list(args))
        if args[:2] == ["volume", "inspect"]:
            name = args[-1]
            if name not in self.volumes:
                return subprocess.CompletedProcess(args, 1, b"", b"no such volume")
            return subprocess.CompletedProcess(args, 0, json.dumps(self.volumes[name]).encode(), b"")
        if args[:2] == ["volume", "rm"]:
            if args[-1] in self.stuck:
                return subprocess.CompletedProcess(args, 1, b"", b"volume is in use")
            self.volumes.pop(args[-1], None)
            return subprocess.CompletedProcess(args, 0, b"", b"")
        if args[:2] == ["volume", "ls"]:
            wanted = args[args.index("--filter") + 1].removeprefix("label=")
            label, _, value = wanted.partition("=")
            names = [name for name, labels in self.volumes.items() if labels.get(label) == value]
            return subprocess.CompletedProcess(args, 0, "".join(f"{name}\n" for name in names).encode(), b"")
        raise AssertionError(f"unexpected docker call {args}")


KEY = "ab" * 32
VOLUME = node_build.volume_name(KEY)
LABELLED = {"faultforge.depcache": KEY[:16]}


def test_the_cache_key_follows_the_dependency_files_and_the_toolchain_not_the_source(project: Path, tmp_path: Path) -> None:
    state = tmp_path / "state"
    linted = node_build.lint(project, BUILD)
    key = node_build.cache_key("sha256:toolchain", linted, state)
    (project / "src").mkdir()
    (project / "src" / "a.ts").write_text("export {}", encoding="utf-8")
    assert node_build.cache_key("sha256:toolchain", node_build.lint(project, BUILD), state) == key
    assert node_build.cache_key("sha256:other", linted, state) != key
    (project / "pnpm-workspace.yaml").write_text("allowBuilds:\n  esbuild: false\n", encoding="utf-8")
    assert node_build.cache_key("sha256:toolchain", node_build.lint(project, BUILD), state) != key
    shutil.copy(DEMO / "pnpm-workspace.yaml", project / "pnpm-workspace.yaml")
    with (project / "pnpm-lock.yaml").open("a", encoding="utf-8") as lockfile:
        lockfile.write("\n")
    assert node_build.cache_key("sha256:toolchain", node_build.lint(project, BUILD), state) != key
    # Two state directories on one daemon never share a volume, so neither removes the other's.
    assert node_build.cache_key("sha256:toolchain", linted, tmp_path / "other-state") != key
    assert node_build.volume_name(key) == f"ff-deps-{key[:16]}"


def test_a_half_written_volume_is_removed_by_name_and_an_orphan_entry_is_dropped(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    state = tmp_path / "state"
    # A volume with no index entry: a fetch or link was killed part way.
    docker = FakeDocker({VOLUME: dict(LABELLED), "someone-elses": {}})
    monkeypatch.setattr(node_build, "_docker", docker)
    assert node_build.cache_ready(state, KEY) is False
    assert ["volume", "rm", VOLUME] in docker.calls and VOLUME not in docker.volumes and "someone-elses" in docker.volumes
    # Complete: the index entry is written last.
    docker.volumes[VOLUME] = dict(LABELLED)
    node_build._write_index(state, KEY, {"packages": 3})
    docker.calls.clear()
    assert node_build.cache_ready(state, KEY) is True and not any(call[:2] == ["volume", "rm"] for call in docker.calls)
    assert node_build.cache_keys(state) == [KEY]
    # An index entry with no volume is dropped, and nothing is removed.
    del docker.volumes[VOLUME]
    assert node_build.cache_ready(state, KEY) is False and node_build.cache_keys(state) == []
    assert not any("prune" in item for call in docker.calls for item in call)


def test_a_volume_without_the_cache_label_is_never_removed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    docker = FakeDocker({VOLUME: {"com.example": "theirs"}, "ff-deps-notakey": dict(LABELLED)})
    monkeypatch.setattr(node_build, "_docker", docker)
    assert node_build.remove_cache_volume(node_build.POLICY, VOLUME) is False
    assert node_build.remove_cache_volume(node_build.POLICY, "ff-deps-notakey") is False
    assert node_build.remove_cache_volume(node_build.POLICY, "ff-deps-" + "0" * 16) is True  # already gone
    assert set(docker.volumes) == {VOLUME, "ff-deps-notakey"} and not any(call[:2] == ["volume", "rm"] for call in docker.calls)


def test_only_the_five_most_recently_used_keys_are_kept(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    state = tmp_path / "state"
    keys = [f"{index:02x}" * 32 for index in range(7)]
    docker = FakeDocker({node_build.volume_name(key): dict(LABELLED) for key in keys})
    monkeypatch.setattr(node_build, "_docker", docker)
    for age, key in enumerate(keys):
        node_build._write_index(state, key, {})
        os.utime(node_build._index_path(state, key), (1_000_000 + age, 1_000_000 + age))
    os.utime(node_build._index_path(state, keys[0]), (2_000_000, 2_000_000))  # the oldest, used again
    removed = node_build.evict(state)
    assert sorted(removed) == sorted(keys[1:3])
    assert set(docker.volumes) == {node_build.volume_name(key) for key in [keys[0], *keys[3:]]}
    assert node_build.cache_keys(state)[0] == keys[0] and len(node_build.cache_keys(state)) == 5


def test_a_build_with_no_toolchain_image_fails_without_touching_anything(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = []

    def docker(policy, args, timeout_s=None, **_):
        calls.append(list(args))
        return subprocess.CompletedProcess(args, 1 if args[:2] == ["image", "inspect"] else 0, b"", b"")

    monkeypatch.setattr(node_build, "_docker", docker)
    built = node_build.build_stack_image(DEMO, BUILD, tmp_path / "state", allow_fetch=True)
    assert built.image_id is None and built.failure_reason == "image_missing" and "build_node_images.sh 24.22.3 12.21.0" in built.failure_detail
    assert all(call[0] == "image" for call in calls)


def test_a_cold_cache_without_permission_never_starts_a_container(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = []

    def docker(policy, args, timeout_s=None, **_):
        calls.append(list(args))
        if args[:2] == ["image", "inspect"]:
            return subprocess.CompletedProcess(args, 0, b"sha256:" + b"1" * 64 + b"\n", b"")
        if args[0] in ("image", "ps"):
            return subprocess.CompletedProcess(args, 0, b"", b"")
        if args[:2] == ["volume", "inspect"]:
            return subprocess.CompletedProcess(args, 1, b"", b"")
        raise AssertionError(f"unexpected docker call {args}")

    monkeypatch.setattr(node_build, "_docker", docker)
    monkeypatch.setattr(node_build, "_remove_sandbox", lambda policy, sandbox_id: 0)
    built = node_build.build_stack_image(DEMO, BUILD, tmp_path / "state", allow_fetch=False)
    assert built.failure_reason == "install_cache_missing" and built.image_id is None and built.cache_key
    assert not any(call[0] in ("run", "network", "build") or call[:2] == ["volume", "create"] for call in calls)
    # And a project the lint refuses fails before the cache is even looked at.
    bad = tmp_path / "bad"
    shutil.copytree(DEMO, bad, ignore=shutil.ignore_patterns("node_modules", "build", ".svelte-kit"))
    (bad / ".pnpmfile.cjs").write_text("", encoding="utf-8")
    calls.clear()
    refused = node_build.build_stack_image(bad, BUILD, tmp_path / "state", allow_fetch=True)
    assert refused.failure_reason == "unsupported_build" and refused.failure_detail == node_build.NEEDS_SCRIPTS
    assert not any(call[0] in ("run", "network", "volume", "build") for call in calls)


def test_the_proxy_log_is_counted_not_quoted() -> None:
    log = b"\n".join([
        json.dumps({"listening": "172.20.0.2", "port": 3128}).encode(),
        json.dumps({"allowed": True, "host": "registry.npmjs.org", "port": 443, "bytes_received": 10, "bytes_sent": 1}).encode(),
        json.dumps({"allowed": True, "host": "registry.npmjs.org", "port": 443, "bytes_received": 5, "bytes_sent": 1}).encode(),
        json.dumps({"allowed": False, "reason": "host_not_allowed", "host": "nodejs.org", "port": 443}).encode(),
        b"not json \x1b[31m",
    ])
    assert node_build.proxy_summary(log) == ("registry.npmjs.org:443: 2 connections, 15 bytes received", "refused nodejs.org:443: 1")


# The output validator, reused


def test_a_hard_link_in_a_node_modules_tree_is_refused(tmp_path: Path) -> None:
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w") as archive:
        first = tarfile.TarInfo("node_modules/.pnpm/pg@8/node_modules/pg/index.js")
        first.size = 2
        archive.addfile(first, io.BytesIO(b"ok"))
        link = tarfile.TarInfo("node_modules/pg/index.js")
        link.type, link.linkname = tarfile.LNKTYPE, "node_modules/.pnpm/pg@8/node_modules/pg/index.js"
        archive.addfile(link)
    stream.seek(0)
    with pytest.raises(UnsafeOutput):
        extract_output(stream, tmp_path / "out", node_build.MAX_OUTPUT_BYTES)


def test_a_relative_link_inside_node_modules_is_kept(tmp_path: Path) -> None:
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w") as archive:
        first = tarfile.TarInfo("node_modules/.pnpm/pg@8/node_modules/pg/index.js")
        first.size = 2
        archive.addfile(first, io.BytesIO(b"ok"))
        link = tarfile.TarInfo("node_modules/pg")
        link.type, link.linkname = tarfile.SYMTYPE, ".pnpm/pg@8/node_modules/pg"
        archive.addfile(link)
    stream.seek(0)
    extract_output(stream, tmp_path / "out", node_build.MAX_OUTPUT_BYTES)
    assert (tmp_path / "out" / "node_modules" / "pg" / "index.js").read_bytes() == b"ok"


# The proxy


class Upstream:
    """Stands in for the registry: echoes what it's sent, on loopback."""

    def __init__(self):
        self.server = socket.create_server(("127.0.0.1", 0))
        self.connections = 0
        threading.Thread(target=self._serve, daemon=True).start()

    @property
    def port(self) -> int:
        return self.server.getsockname()[1]

    def _serve(self) -> None:
        while True:
            try:
                client, _ = self.server.accept()
            except OSError:
                return
            self.connections += 1
            threading.Thread(target=self._echo, args=(client,), daemon=True).start()

    def _echo(self, client: socket.socket) -> None:
        with client:
            while data := client.recv(4096):
                client.sendall(b"echo:" + data)


@pytest.fixture
def proxy():
    upstream = Upstream()
    answers = {"addresses": ["104.16.0.35"]}
    connected: list[tuple[str, int]] = []

    def resolve(host: str, port: int) -> list[str]:
        return list(answers["addresses"])

    def connect(address: str, port: int) -> socket.socket:
        connected.append((address, port))
        return socket.create_connection(("127.0.0.1", upstream.port), 5)

    server = egress_proxy.listener("127.0.0.1", 0)
    threading.Thread(target=egress_proxy.serve, args=(server, resolve, connect), daemon=True).start()
    yield {"port": server.getsockname()[1], "answers": answers, "connected": connected, "upstream": upstream}
    server.close()
    upstream.server.close()


def ask(port: int, request: bytes, then: bytes = b"") -> bytes:
    with socket.create_connection(("127.0.0.1", port), 5) as client:
        client.settimeout(5)
        client.sendall(request)
        reply = client.recv(4096)
        if then and reply.startswith(b"HTTP/1.1 200"):
            client.sendall(then)
            reply += client.recv(4096)
        return reply


def test_the_proxy_forwards_the_registry_to_the_address_it_checked(proxy) -> None:
    reply = ask(proxy["port"], b"CONNECT registry.npmjs.org:443 HTTP/1.1\r\nHost: registry.npmjs.org:443\r\n\r\n", b"hello")
    assert reply.startswith(b"HTTP/1.1 200 Connection established\r\n\r\n") and reply.endswith(b"echo:hello")
    assert proxy["connected"] == [("104.16.0.35", 443)]


@pytest.mark.parametrize("request_line", [
    b"CONNECT nodejs.org:443 HTTP/1.1", b"CONNECT registry.npmjs.org:80 HTTP/1.1", b"CONNECT registry.npmjs.org.evil.example:443 HTTP/1.1",
    b"CONNECT 104.16.0.35:443 HTTP/1.1", b"CONNECT registry.npmjs.org HTTP/1.1", b"CONNECT registry.npmjs.org:443x HTTP/1.1",
    b"GET http://registry.npmjs.org/pg HTTP/1.1", b"GET / HTTP/1.1", b"POST https://registry.npmjs.org/ HTTP/1.1", b"\xff\xfe garbage",
])
def test_the_proxy_refuses_every_other_host_port_and_method(proxy, request_line: bytes) -> None:
    reply = ask(proxy["port"], request_line + b"\r\n\r\n")
    assert reply.startswith((b"HTTP/1.1 403", b"HTTP/1.1 405")) and b"200" not in reply
    assert proxy["connected"] == [] and proxy["upstream"].connections == 0


@pytest.mark.parametrize("address", [
    "10.0.0.1", "127.0.0.1", "169.254.1.1", "169.254.169.254", "192.168.1.1", "172.16.0.1", "100.64.0.1", "0.0.0.0", "224.0.0.1",
    "240.0.0.1", "fe80::1", "fd00::1", "::1", "::", "::ffff:10.0.0.1", "::ffff:127.0.0.1", "::ffff:104.16.0.35", "::10.0.0.1",
    "64:ff9b::a00:1", "2002:a00:1::1", "ff02::1", "not-an-address",
])
def test_the_proxy_refuses_a_name_that_resolves_to_a_private_address(proxy, address: str) -> None:
    assert egress_proxy.is_public_address(address) is False
    proxy["answers"]["addresses"] = [address]
    reply = ask(proxy["port"], b"CONNECT registry.npmjs.org:443 HTTP/1.1\r\n\r\n")
    assert reply.startswith(b"HTTP/1.1 502") and proxy["connected"] == [] and proxy["upstream"].connections == 0


def test_the_proxy_takes_a_public_answer_past_a_private_one(proxy) -> None:
    assert egress_proxy.is_public_address("104.16.0.35") and egress_proxy.is_public_address("2606:4700::6810:23")
    proxy["answers"]["addresses"] = ["10.0.0.1", "2606:4700::6810:23"]
    assert ask(proxy["port"], b"CONNECT registry.npmjs.org:443 HTTP/1.1\r\n\r\n").startswith(b"HTTP/1.1 200")
    assert proxy["connected"] == [("2606:4700::6810:23", 443)]


def test_the_proxy_listens_only_where_it_was_told(proxy) -> None:
    with pytest.raises(OSError):
        socket.create_connection(("127.0.0.2", proxy["port"]), 2).close()
    for everywhere in ("0.0.0.0", "::"):
        with pytest.raises(ValueError):
            egress_proxy.listener(everywhere, 0)
    assert egress_proxy.parse_connect(b"CONNECT Registry.NPMJS.org:443 HTTP/1.1\r\n\r\n") == ("registry.npmjs.org", 443)


# The reaper's networks


def test_the_reaper_removes_only_labelled_networks_whose_expiry_has_passed(monkeypatch: pytest.MonkeyPatch) -> None:
    networks = {
        "a" * 64: {"faultforge.sandbox": "sb-1", "faultforge.expires": "100"},       # expired: removed
        "b" * 64: {"faultforge.sandbox": "sb-2", "faultforge.expires": "999999"},    # live
        "c" * 64: {"faultforge.sandbox": "sb-3"},                                    # no expiry: left alone
        "d" * 64: {"faultforge.sandbox": "sb-4", "faultforge.expires": "soon"},      # malformed: left alone
        "e" * 64: {"faultforge.expires": "100"},                                     # not a sandbox network
    }
    calls: list[list[str]] = []

    def docker(policy, args, timeout_s=None, **_):
        calls.append(list(args))
        if args[:2] == ["network", "ls"]:
            assert "label=faultforge.sandbox" in args
            return subprocess.CompletedProcess(args, 0, "\n".join(networks).encode(), b"")
        if args[:2] == ["network", "inspect"]:
            return subprocess.CompletedProcess(args, 0, "\n".join(f"{key} {json.dumps(labels)}" for key, labels in networks.items()).encode(), b"")
        if args[:2] == ["network", "rm"]:
            return subprocess.CompletedProcess(args, 0, b"", b"")
        raise AssertionError(args)

    monkeypatch.setattr(sandbox, "_docker", docker)
    assert sandbox.reap_stale_networks(now=5000.0) == 1
    assert [call for call in calls if call[:2] == ["network", "rm"]] == [["network", "rm", "a" * 64]]
    assert not any("prune" in item for call in calls for item in call)


def test_a_half_written_volume_that_cant_be_removed_is_never_fetched_over(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    state = tmp_path / "state"
    docker = FakeDocker({VOLUME: dict(LABELLED)}, stuck=(VOLUME,))
    monkeypatch.setattr(node_build, "_docker", docker)
    assert node_build.cache_ready(state, KEY) is False
    # Still there: the build must see it and stop before creating or fetching anything.
    assert VOLUME in docker.volumes and node_build._volume_labels(node_build.POLICY, VOLUME) is not None
    source = (ROOT / "src" / "faultforge" / "node_build.py").read_text(encoding="utf-8")
    guard = source.index("a half-written install cache couldn't be removed")
    assert guard < source.index('"volume", "create"') and guard > source.index("if cache_ready(state, key, policy):")


def test_an_orphan_volume_of_this_state_dir_is_swept_and_no_other_is(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    state, other = tmp_path / "state", tmp_path / "other"
    mine, theirs = node_build.state_tag(state), node_build.state_tag(other)
    kept_key, orphan_key, foreign_key = "aa" * 32, "bb" * 32, "cc" * 32
    def volume(key: str, tag: str) -> dict:
        return {"faultforge.depcache": key[:16], node_build.DEPCACHE_STATE_LABEL: tag}
    docker = FakeDocker({
        node_build.volume_name(kept_key): volume(kept_key, mine),
        node_build.volume_name(orphan_key): volume(orphan_key, mine),  # its fetch was killed, never retried
        node_build.volume_name(foreign_key): volume(foreign_key, theirs),  # another state dir's, no index here
        "someone-elses": {node_build.DEPCACHE_STATE_LABEL: mine},  # not a cache volume name
    })
    monkeypatch.setattr(node_build, "_docker", docker)
    node_build._write_index(state, kept_key, {})
    assert node_build.evict(state) == [node_build.volume_name(orphan_key)]
    assert set(docker.volumes) == {node_build.volume_name(kept_key), node_build.volume_name(foreign_key), "someone-elses"}
    assert not any("prune" in item for call in docker.calls for item in call)


def test_neither_network_has_ipv6() -> None:
    for argv in node_build.network_argv("sb-x", 99).values():
        assert "--ipv6=false" in argv


def test_the_build_scripts_leave_out_attestations_so_a_cached_rebuild_keeps_its_id() -> None:
    """The install cache is keyed on the toolchain image's ID. A provenance attestation records when
    the build ran, so with one the ID changed on every run of the scripts, and the cache went cold."""
    scripts = ROOT / "scripts"
    images = (scripts / "build_images.sh").read_text(encoding="utf-8")
    assert "stable=(--provenance=false --sbom=false)" in images
    builds = [line for line in images.splitlines() if line.lstrip().startswith("docker build")]
    assert len(builds) == 4 and all('"${stable[@]}"' in line for line in builds)
    node = [line for line in (scripts / "build_node_images.sh").read_text(encoding="utf-8").splitlines() if line.lstrip().startswith("docker build")]
    assert len(node) == 1 and "--provenance=false" in node[0] and "--sbom=false" in node[0]
