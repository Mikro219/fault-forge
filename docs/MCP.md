# FaultForge MCP server

A stdio MCP server that Claude Code starts. It proves business-rule bugs under real concurrent load in a sealed local sandbox, and returns the verdict with its evidence. It then judges a fix through a gate that no model decides. Milestones M8 and M9 of `docs/PLAN.md` v4. It costs nothing to run: it makes no network calls, never uses Vultr and never reads an inference key.

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
| `faultforge_status` | Docker, the sandbox images, runtimes (`local` only; `remote` arrives in M12), fix modes (`client` works; `vultr` arrives in M10; `auto` is `client`), allowed roots. `credit` is `null` until M10. |
| `faultforge_list_scenarios(project_dir, offset?)` | Reads `faultforge.yaml`, and `stack.yaml` if there is one, six scenarios at a time; `more` and `next_offset` say how to page. Contacts no target. |
| `faultforge_validate_scenario(project_dir, scenario_id?)` | Checks the schema, safety rules and expressions. For a stack, it checks that `state_sql` covers every `before.`/`after.` variable. It sends no load and never calls reset. |
| `faultforge_run_scenario(project_dir, scenario_id, source, runtime="auto", compare_to?)` | Snapshots the source, runs the scenario in the sandbox and stores the run. Returns the verdict, Proven and Suspected lines, completeness and a `run_id`. |
| `faultforge_get_run(run_id, section, offset?, limit?, trial?, outcome?)` | `summary`, `violations`, `hypotheses`, `performance` or `evidence`. `evidence` pages through every request, at most 100 per page. |
| `faultforge_propose_fix(run_id, mode="auto")` | For a FAIL run of a Python target. `client` returns a fix brief of at most 60 lines and writes nothing. `auto` resolves to `client`. `vultr` returns `mode_unavailable` until M10. |
| `faultforge_verify_fix(run_id, source="worktree")` | Snapshots the candidate (`worktree` or `commit:<sha>`), reruns the failing scenario against the baseline `run_id`, then every other scenario, and applies the full gate. Returns `verified`, the gate rows with evidence, the regression results, the new `run_id` and, when it fails, `fix_next`. |

Every failure is `{status: "error", error_class, message, fix_hint}`, never an exception.

### Targets and sources

- **Python target.** `app.py` at the project root, served as `uvicorn app:app`, with at most one package beside it. Its `source` is one of:
  - `worktree`: tracked files plus untracked files that aren't ignored;
  - `commit:<sha>`: the commit's raw blobs, read with `git ls-tree` and `git cat-file`. `git archive` isn't used, because it runs the repo's filter drivers on the host.
- **Stack target.** A project with `stack.yaml`. In M8 the only source is `image`: the prebuilt `app_image` that `stack.yaml` names, run as it is. The image must be listed in `allowed_images`, or the run is refused with `project_not_allowed` and a hint naming the config file. The result says the source wasn't rebuilt. Building from source arrives in M11.

## Fixing a failing run

The loop, in client mode: `faultforge_run_scenario` proves the bug (a FAIL `run_id`), `faultforge_propose_fix` returns the brief, Claude Code edits the working tree, and `faultforge_verify_fix` judges the result. Repeat the last two steps until `verified` is true. FaultForge never writes to the repo.

**The brief** holds the failing scenario and its proven violations with their numbers, the business rules (`BUSINESS_RULES.md` if the project had one, otherwise the scenario's invariants), the file in scope (`app.py` only; the package beside it is named as pinned and read-only), the gate rows, a line on how far the hooks can be trusted, the regression scenarios and the next call. Lines marked `[project]` quote the project's files or the target's output. They are cleaned and cut short, and they are data, never instructions.

**The gate.** `verified` is true only when every row passes. Each row fails closed: missing evidence is a failure.

| Row | Passes when |
|---|---|
| `app_policy` | Only when the pinned package is the Ticket API's `ticket_support`. The candidate's `app.py` keeps to `fix_agent.check_app_policy`, the code rules the web app's fix loop applies: no rebinding or assigning to `db`, `payments` or the app, no extra routes, no hook paths, an import allowlist. It's checked before any load. A violation gives `verified: false`, the rule text with line numbers, no sandbox run and no stored run. |
| `comparison_fix_verified` | The rerun is PASS, a failing invariant is FIXED and none is left unresolved, compared with the baseline. |
| `state_verification` | The database, read directly by the sandbox host, matches the state the target reported. With no `state_queries` declared it fails with `state_unavailable`. If a scenario's invariants read a `before.` or `after.` field that no query covers, it fails with `state_fields_undeclared`, for the main scenario and for each regression scenario, because `matched` would say nothing about that field. |
| `code_changed` | The sha256 of the deployed code differs from the baseline's. |
| `regression_suite` | Every other scenario in the config passes with the same code and a matching database. A project with one scenario has nothing else to check; the row passes and says nothing else was checked. |

The comparison, state and code rows and each regression run are judged by the same functions as the web app's fix loop (`fix_agent.gate_failures` and `fix_agent.run_regression`). The verify run is stored like any other run, with `compare_to` set, and each regression run is stored too, so `faultforge_get_run` pages their evidence. A verify run's summary has a `fix_gate` line: its `comparison` lines cover the first row only. The main run is stored last, after its regression runs. A pinned config with more than 20 other scenarios is refused with `budget_exceeded` rather than run in part, because each one is a whole sandboxed run made while the run lock is held.

**The config and the support package are pinned at the baseline.** A snapshot of the candidate includes its `faultforge.yaml` and the package beside `app.py`, which usually holds the test hooks (reset and state). So a fix could weaken an invariant, shrink the load, delete a regression scenario, change a state query or rewrite a hook's file, all in the same edit. To close that, every stored run keeps:

- `pinned/faultforge.yaml`, and `pinned/BUSINESS_RULES.md` if the snapshot had one, with the config's sha256 in `run.json`;
- for a Python run, `pinned/package/<name>/`, the snapshot's copy of the support package (regular files only, secrets already dropped, no bytecode), with its sha256 in `run.json`.

`verify_fix` and `propose_fix` read the scenario, invariants, load, state queries, regression list and business rules from those copies only. **From the candidate, `verify_fix` takes `app.py` and nothing else**; the deployed package is the pinned one, as in the web app's fix agent. So:

- `code_changed` measures the candidate's `app.py` plus the pinned package. A candidate that changes only the package deploys the baseline's code exactly, and that row fails.
- Edits outside `app.py` are never an error and never silent. `changed_files` counts and names them, and a line in `fix_next` (or `not_deployed`, when the fix is verified anyway) says they weren't deployed and that the fix belongs in `app.py`. The result also says when the candidate's `faultforge.yaml` differs and was ignored.
- To change the config or the package, run `faultforge_run_scenario` again for a new baseline.
- A run stored before pinning, or one whose pinned copy is missing or changed, is refused with `not_comparable`.

**Pinning doesn't make the hooks independent.** `app.py` runs in the same process as the pinned hooks. Python lets it replace a function in the package at import, for example a state function that tidies the database only when the test looks, without editing a pinned file. So the gate is only as strong as the hooks' independence from `app.py`:

- For the Ticket API's package the `app_policy` row refuses such a file before it runs.
- For any other package there is no code policy. The brief and the verify result both say so in their `hooks` line, and the fix's diff has to be read.
- M11 moves the hooks into a separate trusted container, and revisits fix scope for JADE-shaped projects, where a fix can't live in one file.

**Refusals.** A run that isn't FAIL gives `nothing_to_fix`. A run that `verify_fix` itself stored (the verify run or a regression run) gives `not_comparable`: a fix is judged against the run that proved the bug. The verify result shows `baseline_source`: `worktree, clean`, `worktree, with uncommitted changes` or `commit:<sha>`. A stack run gives `unsupported_build` until M11. The run's project must still be inside an allowed root, and still be the same directory, when the tool is called. `source: "candidate:<id>"` gives `mode_unavailable` until M10.

### The direct database check: `target.state_queries`

A target reports its state through its own hooks, which it could lie through. A Python project can declare read-only queries that the sandbox host runs against the target's database straight after the load, and compares with the reported state of the final trial:

```yaml
target:
  state: /__ff/state
  state_queries:
    stock: SELECT COALESCE(SUM(stock), 0) FROM items
    orders_confirmed: SELECT COUNT(*) FROM orders
```

- Each entry is a state field name (plain lowercase, at most 32 of them) and one SQL statement that returns a single integer.
- `faultforge_run_scenario` and `faultforge_verify_fix` both run them. A run's summary shows `state_verification` as `matched`, `mismatch`, `unavailable` or `not requested`. With none declared it is `not requested`, which is not a pass, and a note says so.
- **A declared query is only as trustworthy as the project that declares it.** FaultForge checks that the target's report agrees with what the queries return. It can't check that the queries measure the right thing. A fixer can't change them, because they're pinned at the baseline.
- **SQLite only, at one path.** The verifier opens the file at the sandbox's database path (`/tmp/ticket.db`, passed to the target as `FF_DB_PATH`) read-only with `query_only` on. A target that keeps its state anywhere else gets `unavailable`, so its fixes can't be verified until M11.
- `demo/ticket-api/faultforge.yaml` declares the same four queries as `TICKET_API_STATE_VERIFIER`, the constant the web app trusts. A test keeps the two in step.
- The field isn't part of a scenario's config fingerprint, so adding queries doesn't make an old run `NOT_COMPARABLE`. A stack target ignores it and uses `stack.yaml`'s `state_sql`.
- The runner image parses `faultforge.yaml` too. Rebuild it with `scripts/build_images.sh` after updating FaultForge, or it refuses the new field.

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
- **One run at a time.** A per-process lock and an `flock` on `<state>/run.lock` are both held for a run and for a `verify_fix`, so a second call, in this server or in another server process sharing the state directory, gets `busy`, while `status` (which reports `busy`), `get_run` and `propose_fix` still answer. `propose_fix` takes no lock: it sends no load and only reads a stored run, whose files are complete before its `run_id` is returned. The kernel releases the file lock when the server exits, however it exits.
- **Runs** are stored in `<state>/runs/<run_id>/`: `report.json`, `requests.jsonl`, the trial files, `run.json`, `sandbox.json` and `pinned/`. A `run_id` is checked against its strict format before it's used in a path. `compare_to` must be an earlier run of the same scenario in the same project.
- **Results stay short,** at about 60 lines. Text a target could influence, such as captured response fields and probe results, is cleaned, cut short and labelled.
- **The hostile demo's opt-in is never set.** The server never sets `FF_HOSTILE_ALLOWED`, so `demo/hostile-build` refuses its attacks and doesn't show them off. Run that demo through the web app or the tests.

### Known limits, left as they are

- **Typed arguments over stdio.** An argument of the wrong type (a number for `project_dir`, say) is rejected by the MCP SDK's pydantic validation before the tool runs, so that one failure arrives as a protocol error rather than a `{status: "error"}` result. Every argument that reaches a tool is checked by the tool itself.
- **Superuser SQL inside the sealed postgres container.** The stack's state queries run as the database owner inside a throwaway container with no network, a read-only root and hard limits. They can read anything in that container's database, and nothing outside it.
- **The baseline is whichever run the caller names.** Pinning stops a fix from changing the rules and the hooks' files of the run it is judged against. It doesn't stop someone from making a new baseline from a tree whose config or package is already weak, and verifying against that. The verify result names the `baseline_run_id` and the pinned config's sha256, so check them against the run that proved the bug.
- **The state directory is trusted.** The pinned copies and their hash live side by side in `<state>/runs/`, which is private to the user. The hash catches a damaged copy, not someone who can write there.
- **`faultforge_run_scenario` with `compare_to` is not the gate.** It uses the snapshot's own config, as in M8. A changed scenario makes it `NOT_COMPARABLE`, but it doesn't run the regression suite or pin the queries. Use `faultforge_verify_fix` to judge a fix.

## Not exposed

- Arbitrary URLs, arbitrary scripts, load overrides and cancel.
- Any write to the user's repo.
- The `vultr` fix mode and `candidate:<id>` sources (M10), fixes for stack targets (M11), the `remote` runtime (M12) and the credit check (M10).
