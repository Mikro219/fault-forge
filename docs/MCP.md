# FaultForge MCP server

A stdio MCP server that Claude Code starts. It proves business-rule bugs under real concurrent load in a sealed local sandbox, and returns the verdict with its evidence. Milestone M8 of `docs/PLAN.md` v4. It costs nothing to run: it makes no network calls, never uses Vultr and never reads an inference key.

## Install

```bash
uv sync                      # the dev group includes the mcp SDK; or: uv sync --extra mcp
bash scripts/build_images.sh # faultforge-target:dev, faultforge-runner:dev, faultforge-stack-hooks:dev
```

Allow the projects the server may touch. With no file, every project is refused.

```toml
# ~/.config/faultforge/mcp.toml
allowed_roots = ["/home/mikep/projects/fault-forge/demo"]
allowed_images = ["faultforge-sportsdeck:buggy"]
```

`project_dir` must resolve, with symlinks followed, inside one of these roots. A stack project may only run an `app_image` listed in `allowed_images`; the default is empty, so no image runs until the owner lists it. Set `FF_MCP_CONFIG` to use a different file, and `FF_MCP_STATE_DIR` to move the state directory (by default `~/.local/state/faultforge`).

## `.mcp.json`

```json
{
  "mcpServers": {
    "faultforge": {
      "command": "uv",
      "args": ["run", "--project", "/home/mikep/projects/fault-forge", "faultforge-mcp"]
    }
  }
}
```

## Tools

| Tool | What it does |
|---|---|
| `faultforge_status` | Docker, the sandbox images, runtimes (`local` only; `remote` arrives in M12), fix modes (M9 and M10), allowed roots. `credit` is `null` until M10. |
| `faultforge_list_scenarios(project_dir, offset?)` | Reads `faultforge.yaml`, and `stack.yaml` if there is one, six scenarios at a time; `more` and `next_offset` say how to page. Contacts no target. |
| `faultforge_validate_scenario(project_dir, scenario_id?)` | Checks the schema, safety rules and expressions. For a stack, it checks that `state_sql` covers every `before.`/`after.` variable. It sends no load and never calls reset. |
| `faultforge_run_scenario(project_dir, scenario_id, source, runtime="auto", compare_to?)` | Snapshots the source, runs the scenario in the sandbox and stores the run. Returns the verdict, Proven and Suspected lines, completeness and a `run_id`. |
| `faultforge_get_run(run_id, section, offset?, limit?, trial?, outcome?)` | `summary`, `violations`, `hypotheses`, `performance` or `evidence`. `evidence` pages through every request, at most 100 per page. |

Every failure is `{status: "error", error_class, message, fix_hint}`, never an exception.

### Targets and sources

- **Python target.** `app.py` at the project root, served as `uvicorn app:app`, with at most one package beside it. Its `source` is one of:
  - `worktree`: tracked files plus untracked files that aren't ignored;
  - `commit:<sha>`: the commit's raw blobs, read with `git ls-tree` and `git cat-file`. `git archive` isn't used, because it runs the repo's filter drivers on the host.
- **Stack target.** A project with `stack.yaml`. In M8 the only source is `image`: the prebuilt `app_image` that `stack.yaml` names, run as it is. The image must be listed in `allowed_images`, or the run is refused with `project_not_allowed` and a hint naming the config file. The result says the source wasn't rebuilt. Building from source arrives in M11.

## Limits and safety

- **Snapshots.**
  - A snapshot goes into a fresh private directory, `<state>/snapshots/`, and is removed after the run.
  - It holds regular files only. Symlinks, special files, `.git/` and anything that resolves outside the project are left out.
  - So are secret-looking files at any depth, before anything reads the snapshot: `.env*`, `*.pem`, `*.key`, `*.p12`, `*.pfx`, `id_rsa*`, `id_ed25519*`, `id_ecdsa*`, `.npmrc`, `.netrc` and `.pypirc`.
  - It's capped at 50 MB and 5000 files.
  - Git runs with no system or global config, no lazy fetch, `protocol.allow=never` (so no transport, and no `core.sshCommand` or remote helper from the repo's config, can run on the host), no fsmonitor and a deadline.
  - **The project's git metadata is untrusted.** Before the object store is read, the repository git resolves for the project must be the project's own: its top level is the project or a parent of it (a monorepo), and its git dir and common dir resolve, with symlinks followed, inside that top level or inside an allowed root. A `.git` file whose `gitdir:` points elsewhere, an `objects/info/alternates` or `http-alternates` file, and a partial (promisor) clone are refused with `project_not_allowed`. A plain repo and a `git worktree add` worktree whose main repo is in the root both work.
  - Leftover snapshots and work dirs older than two hours are swept at the next run.
  - The run uses the snapshot's `faultforge.yaml` (and `stack.yaml`), never the live repo's.
- **The sandbox.** The sandbox policy is the same as the CLI's and the web app's (docs/PLAN.md V3.3): no network, a read-only root, non-root, all capabilities dropped and hard limits. Only containers labelled `faultforge.sandbox` are touched. The server never prunes.
- **A project's `stack.yaml` is untrusted here.**
  - The trusted services must be stock `postgres:<tag>` and `redis:<tag>` images.
  - The app may not run as root.
  - Writable paths must be plain paths.
- **One run at a time.** A per-process lock and an `flock` on `<state>/run.lock` are both held for the run, so a second call, in this server or in another server process sharing the state directory, gets `busy`, while `status` (which reports `busy`) and `get_run` still answer. The kernel releases the file lock when the server exits, however it exits.
- **Runs** are stored in `<state>/runs/<run_id>/`: `report.json`, `requests.jsonl`, the trial files, `run.json` and `sandbox.json`. A `run_id` is checked against its strict format before it's used in a path. `compare_to` must be an earlier run of the same scenario in the same project.
- **Results stay short,** at about 60 lines. Text a target could influence, such as captured response fields and probe results, is cleaned, cut short and labelled.
- **The hostile demo's opt-in is never set.** The server never sets `FF_HOSTILE_ALLOWED`, so `demo/hostile-build` refuses its attacks and doesn't show them off. Run that demo through the web app or the tests.

### Known limits, left as they are

- **Typed arguments over stdio.** An argument of the wrong type (a number for `project_dir`, say) is rejected by the MCP SDK's pydantic validation before the tool runs, so that one failure arrives as a protocol error rather than a `{status: "error"}` result. Every argument that reaches a tool is checked by the tool itself.
- **Superuser SQL inside the sealed postgres container.** The stack's state queries run as the database owner inside a throwaway container with no network, a read-only root and hard limits. They can read anything in that container's database, and nothing outside it.
- **Python runs don't do the direct database check.** The Ticket API target reports its state through its hooks, which a target could lie through (it's disclosed). The stack runtime reads the database directly; M9 adds that check to Python runs.

## Not exposed

- Arbitrary URLs, arbitrary scripts, load overrides and cancel.
- Any write to the user's repo.
- `propose_fix` and `verify_fix` (M9 and M10), the `remote` runtime (M12) and the credit check (M10).
