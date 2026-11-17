# FaultForge MCP server

A stdio MCP server that Claude Code starts. It proves business-rule bugs under real concurrent load in a sealed sandbox, on this machine or on a dedicated runner VM, and returns the verdict with its evidence. It then judges a fix through a gate that no model decides. Milestones M8 to M12 of `docs/PLAN.md` v4.

**Network and money.** Runs, briefs and verifying a fix cost nothing and make no network call. Since M10 the server can make two kinds of call, and only these:

- an account read at `api.vultr.com` (`GET /v2/account`), for `faultforge_status` and for `faultforge_propose_fix` in `auto` or `vultr` mode, and only when the billing key file exists;
- a model call to Vultr Serverless Inference (`api.vultrinference.com`), only for a `vultr` fix that the credit floor allowed, and only when the inference key file exists.

With neither key file, the server makes no network call at all, `auto` is `client`, and nothing can be spent. See "The credit floor" below.

Since M12 there is a third: **SSH to the runner VM** named in `[remote]`, only in remote mode (and one ping from `faultforge_status` and from `auto` when every other condition holds). In remote mode the project's snapshot leaves this machine; no key ever does. See "The remote runtime".

## Install

```bash
uv sync                      # the dev group includes the mcp SDK; or: uv sync --extra mcp
bash scripts/build_images.sh # faultforge-target:dev, faultforge-runner:dev, faultforge-stack-hooks:dev,
                             # faultforge-egress-proxy:dev and the Node toolchain (24.22.3, pnpm 12.21.0)
```

`build_images.sh` uses the network: PyPI for the Python images, and `registry.npmjs.org` once for the pnpm tarball, whose sha512 is checked against `docker/pnpm.lock` before it is installed. The server itself never pulls or builds an image.

Allow the projects the server may touch. With no file, every project is refused.

```toml
# ~/.config/faultforge/mcp.toml
allowed_roots = ["/home/mikep/projects/fault-forge/demo"]
allowed_images = ["faultforge-sportsdeck:buggy"]
allow_network_install = false   # the default; see "Building a stack from source"
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
| `faultforge_status` | Docker, the sandbox images, runtimes (`local` and `remote`, with availability and the reason), fix modes, allowed roots. Also `credit_usd` (whole dollars, rounded down, or `null`), `floor_usd: 10`, `credit_age_s`, `credit_reason`, the `ledger` line (FaultForge's own inference spend today and in total) and `effective_modes`: what `auto` resolves to for fixes, with the reason, and for the runtime (`auto -> remote` or `auto -> local`, with the reason). |
| `faultforge_list_scenarios(project_dir, offset?)` | Reads `faultforge.yaml`, and `stack.yaml` if there is one, six scenarios at a time; `more` and `next_offset` say how to page. Contacts no target. |
| `faultforge_validate_scenario(project_dir, scenario_id?)` | Checks the schema, safety rules and expressions. For a stack, it checks that `state_sql` covers every `before.`/`after.` variable. It sends no load and never calls reset. |
| `faultforge_run_scenario(project_dir, scenario_id, source, runtime="auto", compare_to?)` | Snapshots the source, runs the scenario in the sandbox and stores the run. Returns the verdict, Proven and Suspected lines, completeness, a `run_id`, and `runtime`, `runtime_reason` and `source_left_this_machine` (see "The remote runtime"). |
| `faultforge_get_run(run_id, section, offset?, limit?, trial?, outcome?, candidate?)` | `summary`, `violations`, `hypotheses`, `performance`, `evidence`, `diff` or `schema_objects`. `evidence` pages through every request, at most 100 per page. `diff` pages a stored candidate's diff against the baseline's `app.py`. `schema_objects` pages a stack run's database objects: for a verify run, what the fix added or changed. |
| `faultforge_propose_fix(run_id, mode="auto")` | For a FAIL run of a Python target. `client` returns a fix brief of at most 60 lines. `vultr` has a model write `app.py`, judges it with the full gate and returns a diff. `auto` is `vultr` when every condition for it holds, otherwise `client`, and `mode_reason` says why. No mode writes to the repo. |
| `faultforge_verify_fix(run_id, source="worktree", runtime="auto")` | Takes the candidate (`worktree`, `commit:<sha>` or `candidate:<id>`), reruns the failing scenario against the baseline `run_id`, then every other scenario, and applies the full gate. Returns `verified`, the gate rows with evidence, the regression results, the new `run_id` and, when it fails, `fix_next`. `runtime` defaults to the baseline's; `gate_trust` says whether the rows came from this machine. For a stack it also returns `review_required` (see "The review flag"). |

Every failure is `{status: "error", error_class, message, fix_hint}`, never an exception.

### Targets and sources

- **Python target.** `app.py` at the project root, served as `uvicorn app:app`, with at most one package beside it. Its `source` is one of:
  - `worktree`: tracked files plus untracked files that aren't ignored;
  - `commit:<sha>`: the commit's raw blobs, read with `git ls-tree` and `git cat-file`. `git archive` isn't used, because it runs the repo's filter drivers on the host.
- **Stack target.** A project with `stack.yaml`: an app, Postgres and, if the stack names one, Redis. Its `source` is one of:
  - `image`: the prebuilt `app_image` that `stack.yaml` names, run as it is. The image must be listed in `allowed_images`, or the run is refused with `project_not_allowed` and a hint naming the config file. The result says the source wasn't rebuilt. A run from `image` can't be fixed: there is no source to fix against.
  - `worktree` or `commit:<sha>`, when `stack.yaml` has a `build:` section: the app is built from the snapshot (next section). Without a `build:` section these give `unsupported_build`.

### Building a stack from source

```yaml
# stack.yaml
build:
  kind: node-pnpm            # the only kind
  node: 24.22.3              # exact; must equal .nvmrc and engines.node where they exist
  pnpm: 12.21.0              # exact; must equal package.json's packageManager
  output: [build]            # directories the build produces
  runtime_copy: [package.json, migrations]   # files and directories the image also needs
  start: [node, build]       # the image's command; it must begin with node
  prod_dependencies: true    # put a --prod node_modules in the image
  fix_scope: [src/]          # what a fix may change; the default
```

`runtime_copy` is also how a migration runner reaches the image: the demo copies `tools` and `migrations`, and JADE would copy `tools` and the three `src/lib/server/` folders its runner imports.

There is no command field. The build is the package's own `build` script, run as `pnpm run build`, and the toolchain images are named from the two versions (`scripts/build_node_images.sh <node> <pnpm>`), never by the project. The steps:

1. **Lint, on the host.** `package.json`, `pnpm-lock.yaml` and `pnpm-workspace.yaml` must have a shape the builder knows. Every lockfile package is `name@version` with a sha512 integrity and nothing else; the only exception is one `node@runtime:<build.node>` entry, which `devEngines.runtime` produces and which is never fetched. A pnpmfile, `.npmrc`, patches, a workspace, config dependencies, a `pnpm` field, any `link:`, `file:` or `workspace:` dependency, any resolution with a URL or a git repo, any snapshot, snapshot dependency or importer version that doesn't land on one of those checked registry packages, any unknown key, and a workspace file that asks for install scripts (`allowBuilds: true`, `onlyBuiltDependencies`) are all `unsupported_build`, with one fixed sentence. Nothing is ever given network as a fallback. `faultforge_validate_scenario` runs this lint on the dependency files and the pnpmfiles; it copies files only, so a root `node_modules/` or `patches/` directory is caught at run time instead. A project's `.npmrc` is a secret-looking file, dropped from every snapshot, so it never reaches the lint or the build.
2. **Fetch, on a cache miss only: the one step with network.** See below.
3. **Link, on a cache miss only, with no network.** The dev and prod `node_modules` are laid out in the cache volume. No install script runs, ever: a package that needs one simply doesn't get it, and if the build then fails the result says so.
4. **The sealed build.** This is where project code runs, because a build imports what it bundles. It has no network, no secrets, no stack settings, a read-only root, a non-root user, 3 GB, 2 CPUs and an 8-minute limit. The dependency volume is mounted read-only, so a build can't change the cache.
5. **The image.** The build's output and the prod `node_modules` are read as tar streams and rebuilt on the host by a validator (plain files, directories and links that stay inside; no hard links or devices), then copied into `faultforge-node-run:<node>` by a Dockerfile with no `RUN`. The stack runs it by image ID and it is removed after the call.

The run's `deployed_code_sha256` is the host's hash of the source that was built, because every image build has a new ID. `source` in the result carries `rebuilt: true`, that hash and one line of build facts.

**What `allow_network_install` permits.** The dependencies of each lockfile are fetched once and kept in a Docker volume (`ff-deps-<key>`, the 5 most recently used are kept). With `allow_network_install = false`, the default, a lockfile that isn't cached is `unsupported_build` and nothing is fetched. With `true`, `faultforge_run_scenario` may fetch on a miss:

- A `worktree` baseline fetches whatever that working tree's `pnpm-lock.yaml` names, and a working tree is whatever was last edited, by a person or by Claude. So it can fetch **any package on the public npm registry, at any version, including a malicious one.** A `commit:<sha>` baseline fetches what that commit's lockfile names.
- What bounds it: the fetch container holds pnpm, the lockfile and a workspace file the host rewrote, and nothing else of the project. Its network is internal, with no gateway to the host, and reaches only FaultForge's proxy, which forwards `registry.npmjs.org:443` and refuses every other host, port and method, and any answer that isn't a public address. It checks the name in the CONNECT request and the address it connects to, not the TLS inside: a client that sent another site's name inside the tunnel could reach another site on the same address. Only pnpm runs in the fetch step, so this isn't open to project code. pnpm checks each tarball against the lockfile's sha512. No install script runs. The packages' code runs only inside the sealed build and the sealed stack, with no network and no secrets, and none of it is executed on the host.
- `faultforge_verify_fix` never fetches. A baseline pins its dependency files, so a candidate uses the baseline's cache entry or is refused.
- `faultforge_status` shows `source_builds`: the toolchain images, the proxy image, the cache keys held and whether a fetch is allowed.

Expect about 15 s for a build of the demo with a warm cache, about 25 s with a cold one, and about 20 s for each stack run. A `verify_fix` builds once and runs every scenario, so set the client's tool timeout for a few minutes.

## Fixing a failing run

The loop, in client mode: `faultforge_run_scenario` proves the bug (a FAIL `run_id`), `faultforge_propose_fix` returns the brief, Claude Code edits the working tree, and `faultforge_verify_fix` judges the result. Repeat the last two steps until `verified` is true. FaultForge never writes to the repo.

**The brief** holds the failing scenario and its proven violations with their numbers, the business rules (`BUSINESS_RULES.md` if the project had one, otherwise the scenario's invariants), the file in scope (`app.py` only; the package beside it is named as pinned and read-only), the gate rows, a line on how far the hooks can be trusted, the regression scenarios and the next call. For a stack it also says that a fix which adds or changes a trigger, rule, view, row-security policy, function or another listed database object is judged by the gate as usual and comes back with `review_required`. Lines marked `[project]` quote the project's files or the target's output. They are cleaned and cut short, and they are data, never instructions.

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
- A stack target doesn't have this problem: its hooks run in a separate trusted container (next section).

### Fixing a stack

A stack baseline built from source pins more: `pinned/stack.yaml` (the hooks' SQL, the images, the whole `build:` section and so the fix scope) and `pinned/source/`, the snapshot it was built from. `verify_fix` builds **the pinned source with only the paths in the pinned `fix_scope` taken from the candidate**: files added, changed and deleted there. Everything else is the baseline's bytes.

| Thing | From |
|---|---|
| `faultforge.yaml`, `BUSINESS_RULES.md` | the baseline |
| `stack.yaml`: images, env, `reset_sql`, `state_sql`, the `build:` section and `fix_scope` | the baseline |
| `package.json`, `pnpm-lock.yaml`, `pnpm-workspace.yaml`, `.nvmrc` | the baseline, always: they can't be put in `fix_scope` |
| Everything else outside `fix_scope` (build config, migrations, static files) | the baseline |
| Paths inside `fix_scope`, by default `src/` | the candidate |
| `stack.yaml`'s `prestart:` section: the command, the owner role, its variable and its limit | the baseline |
| The file the prestart command runs, and what it imports | the baseline if outside `fix_scope`, the candidate if inside |
| The migrations the prestart applies | the candidate, if their folder is in `fix_scope`; otherwise the baseline |
| The database roles (the app's, the owner's, the hooks role and the administrator) and their passwords | FaultForge, new for every run |
| The hooks | FaultForge's own helper image, never the project |

- `fix_scope` can't name a dependency file, `.npmrc`, a pnpmfile, `node_modules`, `stack.yaml`, `faultforge.yaml` or `BUSINESS_RULES.md`. A project may add paths such as `migrations/` or `vite.config.ts`. So a fix can never add or change a dependency; that needs a new baseline.
- **A fix that changes the schema needs two things.** First, its migrations folder must be in `fix_scope` before the baseline is recorded, because the scope is pinned then. Second, something in the sandbox must apply the migrations: a `prestart` step (next section), or the app itself at start. The redeem demo does the first: `fix_scope: [src/, migrations/]`, and `node tools/migrate.ts` as its prestart. A candidate's migrations are then built into the image and applied by the **pinned** prestart command.
- Edits outside the scope are never an error and never silent: `changed_files` names them and `not_deployed` or `fix_next` says they weren't deployed.
- `code_changed` compares the source hash, and is checked **before any build**: a candidate whose scope is unchanged fails that row at once and nothing is built.
- A candidate whose sealed build fails gives `verified: false` with `build_failed`, the end of the build's output in `build_log` (marked `[project]`: it is the project's text), every row "not run" and no stored run.
- There is no `app_policy` row. That row exists to stop `app.py` wrapping hooks in its own process; a stack's hooks are in another container, and a text rule over TypeScript would only be theatre. The seal is what contains the code.
- A verify with different toolchain images, or after the baseline's install cache entry is gone, is `not_comparable`: make a new baseline.
- The `vultr` mode is `mode_unavailable` for a stack and `auto` picks `client`: a stack fix can span several files, and the model loop writes one `app.py`.

### The prestart step: migrations as the database's owner

```yaml
# stack.yaml
database_user: redeem_app            # the role the app logs in as
prestart:
  command: [node, tools/migrate.ts]  # an argument list for node: plain tokens, no shell
  owner_role: redeem_owner           # must differ from database_user
  owner_url_env: MIGRATION_DATABASE_URL   # the default; the app's URL is always DATABASE_URL
  timeout_s: 30                      # the default; 1 to 60
```

Without `prestart:` a stack is as before: one role, `database_user`, which owns its database. With it:

1. Postgres starts. FaultForge creates **two ordinary roles**, each with its own random password: `owner_role`, which owns the database and its `public` schema, and `database_user`, which owns nothing. `CREATE` on `public` is revoked from `PUBLIC`, so the app role has only what the project's migrations grant it. It also creates its own hooks role (next section).
2. **The prestart runs once**, in the app image (the one just built, or the prebuilt `app_image`), with `node` as its entrypoint and the rest of `command` as its arguments. It is sealed exactly as the app is: the same user, limits, read-only root, dropped capabilities and `no-new-privileges`, in postgres's network namespace and nowhere else, labelled `faultforge.sandbox`. It gets the stack's `env`, `DATABASE_URL` (the app role's) and the owner's URL in `owner_url_env`. It never gets the hooks token or the administrator's password.
3. Its container is read (its settings and exit status go into the run's containment facts) and removed, by its exact name. Only an exit status of 0 inside `timeout_s` lets the stack go on. Anything else is a failed run, `target_start_failed`, never a verdict: the detail says the status or the limit, and quotes the last 200 characters of the command's output on one line, marked `[project]`, with this run's two database passwords taken out.
4. The owner is locked (`NOLOGIN`, its sessions ended), and the catalog inventory is read for the first time (see "The review flag").
5. Then the hooks helper, then the app. The app gets `DATABASE_URL` for the app role and never the owner's URL. The helper reads and resets as the hooks role, with its `search_path` pinned and `row_security` off.

`command` is held to the same rule as `build.start`: it begins with `node`, and every token is plain (`[A-Za-z0-9_./=:@-]`), so there is no shell, quote, space or expansion. The whole section is part of `stack.yaml`, so it is pinned at the baseline: a candidate can't change the command, the roles or the limit.

**The file the command names is pinned only if it is outside `fix_scope`.** The demo's `tools/migrate.ts` is; JADE's `tools/db/migrate.ts` is too, but it imports `src/lib/server/db/`, which a `src/` scope takes from the candidate. Either way it is project code.

**How this maps to JADE** (read from its repo; nothing there was changed). JADE's `node tools/db/migrate.ts` reads `MIGRATION_DATABASE_URL` (a URL for role `jade_owner`) and `DATABASE_URL` (for `jade_app`), which must name the same host, port and database and carry no query string, and `APP_ENV`. Its `stack.yaml` would have `database_user: jade_app`, `prestart: {command: [node, tools/db/migrate.ts], owner_role: jade_owner}` and `APP_ENV: staging` in `env` (its image sets `NODE_ENV=production`, which allows only `staging` and `production`). Three things in its runner were checked against this design:

- Its role bootstrap creates `jade_app` only when the role is missing. FaultForge has already created it with the password in `DATABASE_URL`, so the runner finds it, logs in as it to confirm, and writes nothing. That matters: the owner here can't create a role.
- It takes one advisory lock in the target database and one in the cluster's `postgres` database. An ordinary role can do both; the test suite checks the owner can connect to `postgres` and lock there.
- Its first migration revokes `CREATE` on `public` from `PUBLIC` and grants to `jade_app` by name. The owner owns the schema, so both work.

JADE's runner has not been run in a FaultForge sandbox yet: that needs its own `stack.yaml` and is the JADE bead's work.

### The hooks role: the pinned SQL never runs as a superuser

`reset_sql` and `state_sql` run in two places: in the hooks helper, over TCP, and from the host, through the unix socket inside the postgres container. Both run as **`faultforge_hooks`**, which every stack gets, with or without a prestart:

- It is `LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE`, with a random password of its own that only the helper holds. `faultforge_hooks` and `faultforge_admin` are refused as a `database_user` or an `owner_role`.
- It is **a member of the role that owns the tables**: `owner_role` in a stack with a prestart, `database_user` in one without (SportsDeck, where the app creates its own schema). Membership and not table grants, because `TRUNCATE ... RESTART IDENTITY` needs ownership and the tables don't exist when the roles are made. The grant is one way, with no admin option: the owner gains nothing, and the hooks role can't pass the membership on. Locking the owner (`NOLOGIN`) doesn't touch it; the tests reset and read as the hooks role after the owner is locked.
- **So a trigger, a rule or a view function that fires in a hooks session has the table owner's rights and no more.** A migration's trigger on a table the reset truncates can no longer run `COPY ... TO PROGRAM`, read a server file or change a role: the tests plant one and check the refusal (SQLSTATE 42501), and check that the same trigger in an administrator's session does run the program.
- The administrator (`faultforge_admin`, a superuser) is now used for two things only: creating the roles, and locking the owner after the prestart. It never runs the project's SQL or touches a project table, and the helper no longer holds its password.
- Each session still pins `search_path = pg_catalog, public`.

**Row-level security.** A superuser is never subject to a row-security policy. A member of the table's owner is, when the policy is forced (`ALTER TABLE ... FORCE ROW LEVEL SECURITY`). So moving off the superuser opened a new way to hide rows from the check: a migration forces a policy, and the pinned `COUNT(*)` reads fewer rows than the table holds, with no error. To close it, **every hooks session and every host read sets `row_security = off`**. Postgres then refuses a query that a policy would filter, instead of filtering it. The helper answers 503, the host's read fails, and the state check is `unavailable` (`state_unavailable`), which fails the gate; a reset that a policy would affect fails the run. It is never a forged match. The tests force a policy and check all three, and that without the setting the same role reads 1 row where the table holds 3. A policy that isn't forced doesn't apply to the owner or its members, so nothing is hidden and nothing fails.

**The helper image must be this FaultForge's.** An image built before this change would run the pinned SQL without `row_security = off`. So the first container of every stack run is a check: sealed like the helper, with no network, it compares the sha256 of `/opt/stack_hooks.py` in the image with FaultForge's own copy and removes itself. It runs before postgres, and so before the prestart or any project code. The run is refused (`image_missing`) if the file differs, and the detail says which: another version, a file that can't be read, or a check that couldn't run. Rebuild with `scripts/build_images.sh` after updating FaultForge.

**What the hooks role can still do,** all of it inside the throwaway database: whatever the tables' owner can, and change its own password and its own session defaults. A trigger that did either would lock the helper out or be overridden by the settings each session pins, so the run fails closed. It can't unlock or raise the owner, change another role's password, or create a role. A table that some other role owns (an app role that a migration let create tables, say) is one it can't truncate and may not be able to read: the reset or the state read then fails, and the run fails rather than passes.

### The review flag: database objects a fix added

A migration in the fix scope can add a trigger, a rule, a view, a function, a row-security policy or a child table that makes the state check read the right numbers while the bug remains. No gate row can tell a careful one from an honest one. So FaultForge records what is there and says when a fix changed it. **It flags; it never fails:** `verified` is what the gate rows decide, with or without the flag.

**The inventory.** One `SELECT` over `pg_catalog`, as the hooks role, in a read-only transaction with a 5 s limit. It reads catalog tables and the server's own deparse functions only, so no project code runs. It lists:

| Kind | What |
|---|---|
| `trigger` | every trigger in the app's schemas that isn't internal (a foreign key's own triggers are internal and left out) |
| `rule` | every rule except the `_RETURN` rule each view has |
| `view`, `materialized_view` | every one |
| `policy` | every row-security policy |
| `row_security`, `row_security_forced` | every table with row security enabled, or forced |
| `function`, `procedure`, `aggregate`, `security_definer_function` | **every** one in the app's schemas, whoever's rights it runs with, so a helper whose body changes is seen too |
| `operator`, `cast` | every one made after initdb (OID 16384 or above), in any schema |
| `generated_column` | every generated column |
| `partitioned_table`, `foreign_table`, `child_table` | every partitioned or foreign table, and every partition or inheritance child, with its parent |
| `event_trigger` | every one (only a superuser can make one, so a project can't) |

"The app's schemas" are all but `pg_catalog`, `information_schema` and the server's other `pg_` schemas. Each object is one line, `kind name on schema.table #digest`, sorted, for example `rule audit_log_muted on public.audit_log #f586351012cb`. The digest is 12 hex characters over the object's definition as the server prints it: a view's query, a rule's or trigger's definition and whether it is enabled, a function's body, language, owner, settings and whether it is `SECURITY DEFINER`, a policy's command, roles and conditions, a generated column's expression, a partition's bounds. No OID goes into it, so the same migrations give the same lines in every run. Names are the project's text: made one line of printable ASCII and cut to 80 characters a part.

**The query can't be captured from the app's schema.** `pg_catalog` first on the `search_path` only wins for the same argument types: an aggregate `public.json_agg(json)`, or a function with closer argument types than the built-in's, is chosen over the built-in, and could return an empty list. So the inventory's session has `search_path = pg_catalog` and nothing else, and names every function it calls there. No operator of the app's is visible on that path. The tests plant the overloads and check the list is whole, and that the same query with `public` on its path reads as an empty catalog. The host's state read calls `pg_catalog.json_build_object` by name for the same reason; a planted `public.json_build_object(text, bigint)` forged the count before.

**When it is read.** At the end of every run, straight after the state read. A stack with a prestart is also read before the load: after the prestart has exited and the owner is locked, before the helper and the app start. Both reads matter there. The first is before any project code has run as the hooks role. But a check constraint, a default or a trigger can call a function during the first reset, as the hooks role, and that function can make a view or a trigger. Only the second read sees it. What is stored is both reads together, and the lines only the second had are stored apart as `schema_objects_during`. Without a prestart there is one read, at the end: the app makes its own schema as it starts and only the runner knows when it has.

The list is stored with the run (`schema_objects` and `schema_objects_during` in `run.json` and `sandbox.json`), for the baseline as for any run, and a run's notes say how many were recorded. `faultforge_get_run(run_id, section="schema_objects")` pages it, 40 lines at a time.

**The comparison.** `faultforge_verify_fix` compares the inventory of every run it makes (the main scenario and each regression scenario) with the baseline's:

- **added**: a line whose kind, name and table the baseline doesn't have;
- **changed**: the same kind, name and table with another digest;
- either one that wasn't there before the load says `(appeared during the run)`;
- an object the fix dropped is not flagged: it can't stand between the check and the tables.

Anything added or changed gives:

```json
"review_required": true,
"review": "verified by the gate, but the fix added database objects that can change what the state check reads; read them before trusting the result.",
"schema_objects": [
  "[project] added: function public.audit_log_only_redeems()",
  "[project] added: trigger audit_log_only_redeems on public.audit_log"
],
"review_files": "1 [project]: migrations/0001_one_redemption_per_code.sql"
```

`schema_objects` holds at most 3 lines. When there are more, the last is the `faultforge_get_run(..., section="schema_objects")` call that pages every one of them from the stored verify run. `review_files` names the changed files in the fix scope that are migrations by name (a `.sql` file, or a path containing `migrat`). When the gate rejected the fix, `review` begins "not verified, and" instead. The verify run's stored summary says `review required` on its `fix_gate` line. With nothing added or changed the result has `"review_required": false` and none of the other fields. The fix brief tells Claude this will happen.

**It fails toward the flag.** If the catalog couldn't be recorded, for the baseline or for any run of the verify, `review_required` is true and `review` gives the cause:

- `"review_reason": "couldn't read the catalog"`: a timeout, a failed `psql`, output of the wrong shape, either of a prestart stack's two reads missing, or a baseline stored before inventories were recorded (make a new one);
- `"review_reason": "too many database objects to record"`: more than 1000 objects, or lines adding up to more than 80,000 characters, in one read or in both together. Nothing is recorded in part.

**What it doesn't see.** It lists the kinds above and nothing else. A column default, a check constraint and an index expression aren't lines of their own; the functions they call are, when they are the app's. A check or default written with built-in functions only is not listed. Types, domains and their constraints, collations, sequences, publications, grants and settings on a role or the database are not listed. Without a prestart the app owns its schema, so it could add an object and drop it again before the read at the end of the run; with one, project code running as the hooks role could do the same between the two reads. A legitimate unique index or constraint adds none of the listed objects, so the demo's `reference_fix_schema` comes back with no flag; its `flagged_fix` (the same constraint plus a trigger and its function) is verified and flagged, and `false_fixes/migration_mutes_audit` is rejected by the gate and flagged too.

**Postgres 16 or newer.** Before 16 a role could grant membership in itself, so a migration running as the owner could `GRANT owner TO app` and the app would own the schema after all. A project's `postgres_image` must be named by a version of 16 or above (`postgres:16-alpine`; `postgres:latest` and `postgres:15` are refused as `config_invalid`). The tag is what is checked, not the server inside the image.

**What a verified stack fix still doesn't prove.**

- **Without a prestart, the app owns its own database and schema.** It can replace a table with a view, and it can attach a trigger, a rule or a view over a function to a table the pinned SQL reads or resets. That code then runs when the helper or the host touches the object, **with the hooks role's rights, which are the app role's own** and no superuser's. So an app written to cheat can change what the check reads, and gains nothing else by it.
- **With a prestart, the migrations own it instead.** The app role can't create or alter anything itself. Once the prestart has exited, the owner is set to `NOLOGIN` and any session it left open is ended, so a migration that changed the owner's password, or a second connection it kept, gives the running app nothing. What stays open is what the owner built for the app on purpose: a `SECURITY DEFINER` function a migration created runs with the owner's rights when the app calls it. The prestart's script must sit outside `fix_scope` and the build output and come right after `node`, with no node options; code it imports from the scope (JADE's `tools/db/migrate.ts` imports `src/lib/server/db/`) is the candidate's. The route is open to whatever the prestart runs, which is project code holding the owner's login, and with `migrations/` in `fix_scope` that includes a candidate's migrations. Stated plainly:
  - **The owner can do anything inside the sandbox database.** It can create, alter and drop every object, rewrite or delete any row, and grant the app role anything it has itself. The database is thrown away after each run, in a container with no network.
  - **The owner is not a superuser, and can't become one.** It has `NOSUPERUSER NOCREATEDB NOCREATEROLE` and is a member of no role, so `COPY ... PROGRAM`, server-side file reads, `CREATE ROLE`, `ALTER SYSTEM`, untrusted languages and extensions that need a superuser are all refused; the tests run each and check the refusal. It can't log in as the administrator, or change the administrator's or the app's password.
  - **A hostile migration can still make the hooks lie.** It can replace a table the pinned `state_sql` reads with a view that returns what it likes, add a trigger or a rule to a table `reset_sql` writes (the demo's `migration_mutes_audit` false fix does this, clumsily enough to be caught), or attach a function to either. When the helper or the host then touches that object, the function runs **as the hooks role**, with the owner's rights: it can't run a program, read a server file or change another role (see "The hooks role"). But the numbers the gate reads can be forged this way, and no gate row detects a careful forgery. This is the same class as the trigger and view route above: project code that owns the schema.
  - So a verified fix that includes a migration is strong evidence against an honest mistake, and no evidence against a migration written to fool the check. **Read a candidate's migrations before trusting the result.** `changed_files` names them, and a result with `review_required: true` names the triggers, rules, views, policies, functions and other listed objects the fix added or changed (see "The review flag"). The flag is a prompt to read, not a judgement: only a reader can tell a harmless object from a forgery, and a forgery through something the inventory doesn't list isn't flagged.
- The helper's and the host's sessions pin `search_path = pg_catalog, public`, so the app can't shadow a built-in such as `count(*)` with one of the same name and argument types, and set `row_security = off`, so a forced policy fails the read instead of hiding rows. That closes shadowing by an identical signature and policy filtering, not the route above. **An overload with closer argument types is still chosen over a built-in**, whatever the path: a project's `state_sql` that calls `SUM(x)` on a type the app has defined its own `sum` for would run the app's. FaultForge's own statements name `pg_catalog`; the pinned `state_sql` and `reset_sql` are the project's, and should too where it matters. A function added that way is a line in the inventory. `validate_scenario` warns when a `reset_sql` or `state_sql` statement names a table without a schema.
- Postgres asks for a password on every TCP login (`--auth-host=scram-sha-256`), so the app can't log in as the administrator over the loopback it shares. The host reads through the unix socket inside the postgres container.
- **The hooks token stops accidents, not hostile code.** The helper answers only a caller that holds a per-run token, which the runner has and the app doesn't. An app that calls a `/__ff/` path gets a 403 rather than a reset. It is not a security boundary against an app that owns its schema.
- The app can keep state in memory. Client counts are measured by the runner regardless, so the capacity and liveness invariants don't depend on any of this.
- A build runs project code on the host's kernel, in a container. A kernel escape isn't something these sandboxes stop. Local use only.

## Vultr fixes

`faultforge_propose_fix(run_id, mode="vultr")` runs the web app's fix loop (`fix_agent.run_fix_loop`) on the baseline's pinned inputs: `pinned/app.py`, the pinned package, `faultforge.yaml`, `BUSINESS_RULES.md`, the state queries and the regression list. The model is `glm-5.3` at low reasoning effort, with up to 3 attempts and 8000 output tokens each. Every candidate that keeps to the code rules runs in a new sandbox under the same gate as above, then against every other scenario. The call holds the run lock, since it sends load, and can take several minutes.

**It never writes to the repo.** The result holds:

- `verified` and `stop_reason`, and one line per attempt in `attempts`: the candidate's id, each gate row by name as passed or FAILED, the regression suite and the failure reason, in fixed words;
- `candidate`, as `candidate:<id>`: the verified one, or the last one tried. Each candidate is stored under the baseline run in `<state>/runs/<run_id>/candidates/<id>/`, and its id is the start of its sha256, checked whenever it is read;
- `diff`: the first 20 lines of a unified diff of the baseline's `app.py` against the candidate. `diff_more` is the `faultforge_get_run(..., section="diff")` call that pages the rest;
- `cost_usd`, `credit_usd`, `floor_usd`, the credit before and after, and the `ledger` line.

**The diff is model output, so it is untrusted,** and every line of it is marked `[model]`, as project text is marked `[project]`. FaultForge applies nothing. Read the diff, strip the mark from each line, call `faultforge_verify_fix(run_id, source="candidate:<id>")` to have the stored candidate judged again through the whole gate (the `app_policy` row included), and apply it yourself. Lines keep tabs and printable ASCII; anything else is replaced and the result says the diff is no longer exact.

Each attempt's runs are stored like a verify run, so `faultforge_get_run` pages their evidence.

**When `vultr` is allowed.** All of these must hold, and they are checked in this order, so nothing reaches the network for a run that can't use it:

1. The baseline's pinned package is `ticket_support`. The loop's code policy (`check_app_policy`) is written for that package, so any other project gets `mode_unavailable`.
2. The pinned state queries cover every field the invariants read. Otherwise no candidate could pass the gate, and the money would be wasted: `mode_unavailable`.
3. The baseline has its pinned `app.py`. A baseline stored before M10 doesn't: `not_comparable`. Run the scenario again.
4. The inference key file is there: `mode_unavailable` without it.
5. The credit floor and the ledger allow the worst case: `credit_floor` otherwise, with the reason and `credit_usd`.

`auto` picks `vultr` when all five hold and `client` otherwise. `mode_reason` gives the first reason, for example `auto resolves to client: credit $10.29 is below the $10.00 floor after the worst case ($0.30)`, `no billing key` or `not a ticket_support project`.

## The credit floor

A paid operation is allowed only if `credit - worst_case_cost >= $10.00`.

- **Credit** is `-balance - pending_charges` from `GET https://api.vultr.com/v2/account`, less whatever the ledger recorded after that reading was taken.
- **The floor is a buffer, not a hard limit.** It is there so that a small overspend doesn't matter, and two things can cause one:
  - Vultr's account figure can lag FaultForge's own spend. The ledger only corrects for spend since the last reading, so after a new reading that hasn't caught up, a fix can be allowed that takes the real credit a little under $10.
  - The per-call cap is checked against an estimate before the call is sent. A reply that reports more usage is charged in full, so one fix can cost more than the $0.30 below.

  The ledger's $2 a day and $15 total caps are the outer bound on FaultForge's spend.
- **The planned worst case of a vultr fix is $0.30.** Each model call is capped at $0.10: before a call is sent, its worst case (the prompt's upper bound in tokens plus all 8000 output tokens, at $0.75 and $3.00 per million) must be within the cap, or it isn't sent. There are at most 3 attempts. A fix verified on the first attempt is expected to cost about $0.01 to $0.03 (an estimate from the prompt size, not yet measured).
- **The worst case of a remote run** (M12) is the VM's hourly rate for the call's wall limit, rounded up to whole hours: $0.066 for one run on the runner VM. See "The remote runtime".
- **The reading is cached** in memory with its time, and read again only when `faultforge_status` or a paid decision finds it missing or older than 10 minutes. No other tool reads it: their results carry the last reading if it is still fresh, or `null`.

**It fails closed.** Each of these counts as below the floor, with a short fixed reason and `credit_usd: null`:

- no billing key file;
- negative `pending_charges`, which would raise the credit;
- a key file that is a symlink, not a regular file, readable by group or others, owned by another user, or too large;
- a key file without a usable `VULTR_BILLING_API_KEY` line;
- any HTTP status but 200, a redirect included;
- a timeout or a network error;
- a reply that isn't JSON, has no `account.balance` or `account.pending_charges`, or holds a string, a boolean, `NaN`, an infinity or an absurd number there;
- a reading older than 10 minutes, measured on the monotonic clock, so setting the wall clock back doesn't keep it fresh;
- a ledger that can't be read.

Then `auto` picks `client`, and an explicit `vultr` returns `error_class: credit_floor`.

**The ledger** is the second bound. `<state>/spend.json` holds FaultForge's own MCP inference spend, in numbers only. It is the web app's `BudgetLedger` with the same caps: **$2 a day and $15 in total**, and 1,000,000 tokens a day. Every model call is checked against it before it is sent and recorded after. It is read from disk for every decision, so two servers sharing a state directory see each other's spend, and a vultr fix holds the run lock while it spends. A ledger that can't be read or written stops all spending. A capped ledger refuses without asking the API.

### The key files

| File | Variable | Override |
|---|---|---|
| `~/.config/faultforge/vultr.env` | `VULTR_BILLING_API_KEY` | `FF_VULTR_ENV` |
| `~/.config/faultforge/inference.env` | `VULTR_INFERENCE_API_KEY` | `FF_INFERENCE_ENV` |

```bash
install -m 600 /dev/null ~/.config/faultforge/vultr.env
$EDITOR ~/.config/faultforge/vultr.env   # VULTR_BILLING_API_KEY=...
```

Each file must be a regular file, not a symlink, owned by you, with mode 0600 (no access for group or others). It is read again on every use, never cached and never put in the environment. A key is never passed to a sandbox, logged, stored in the state directory or returned by a tool; errors carry fixed words and an exception's type at most.

**Which Vultr key.** `VULTR_BILLING_API_KEY` only needs to read the account.

- **Preferred:** make a sub-user with only the billing view and use its key. Then the key can do nothing else, whatever holds it.
- **The main account key works too,** and it carries every permission. FaultForge only ever reads `/v2/account` with it, and that is enforced in code, not by convention: the one HTTP client that carries the key sends through a transport that refuses any request except `GET https://api.vultr.com/v2/account` with no query string and no body, before it leaves. Another method, host, port, scheme, path or query is refused. Redirects are never followed, and a 3xx reply counts as a failed check.
- **Either way, restrict the key to this machine's IP** under Vultr's API access control (Account, API, Access Control), so a copy of it is useless elsewhere.

**Refusals.** A run that isn't FAIL gives `nothing_to_fix`. A run that `verify_fix` itself stored (the verify run or a regression run) gives `not_comparable`: a fix is judged against the run that proved the bug. The verify result shows `baseline_source`: `worktree, clean`, `worktree, with uncommitted changes` or `commit:<sha>`. A stack run of a prebuilt image gives `unsupported_build`: make a baseline from source first. The run's project must still be inside an allowed root, and still be the same directory, when the tool is called. A `candidate:<id>` that isn't 12 hex characters gives `invalid_argument`, one that isn't stored for that run gives `run_not_found`, and one whose bytes no longer match its id gives `not_comparable`.

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
- **SQLite only, at one path.** The verifier opens the file at the sandbox's database path (`/tmp/ticket.db`, passed to the target as `FF_DB_PATH`) read-only with `query_only` on. A target that keeps its state anywhere else gets `unavailable`, so its fixes can't be verified; a stack target reads Postgres instead.
- `demo/ticket-api/faultforge.yaml` declares the same four queries as `TICKET_API_STATE_VERIFIER`, the constant the web app trusts. A test keeps the two in step.
- The field isn't part of a scenario's config fingerprint, so adding queries doesn't make an old run `NOT_COMPARABLE`. A stack target ignores it and uses `stack.yaml`'s `state_sql`.
- The runner image parses `faultforge.yaml` too. Rebuild it with `scripts/build_images.sh` after updating FaultForge, or it refuses the new field. The same script rebuilds the stack hooks helper, which a stack run refuses when it isn't this FaultForge's.

## The remote runtime

Since M12 a run can go to a dedicated runner VM instead of this machine's Docker. The snapshot (the allowlist, the secret-looking files dropped, the pinning), the stored runs, every model call and the gate's arithmetic stay here. The sandboxed steps move: a Python run, a stack run, a source build and removing a built image are each **one SSH call** to the VM's one entry point, `faultforge.remote_run`. **On the remote runtime most of the gate's evidence is what the VM reported**: see "What the gate trusts" below.

**What leaves this machine, and only with `runtime: "remote"`** (or `auto` resolving to it): the snapshot of the project (the same files a local run would mount, secrets already dropped), the pinned `faultforge.yaml` and, for a stack, its pinned `stack.yaml` settings, a baseline's `report.json` for a comparison (earlier runs of the same call stay here, and only the step's own run comes back), and fixed request fields. The VM keeps an image built from the source until the end of the call (swept within two hours if removing it fails) and the stack install cache. Never a key: the inference and Vultr keys stay here, no request carries one, and `ssh` gets a minimal environment with no agent socket. The result says so: `runtime: "remote"`, `source_left_this_machine: true` and a plain `remote` line; a stored run's summary shows `runtime` and the same line in its notes. `faultforge_verify_fix` has the same `source_left_this_machine` and `gate_trust` fields and puts the runtime and its reason on its `source` line; to stay within 60 lines its worst result now shows one proven line, four regression lines and two review-object lines (the rest page through `faultforge_get_run`). A local run says `source_left_this_machine: false`.

### What the gate trusts

Every result of `faultforge_run_scenario`, `faultforge_verify_fix` and `faultforge_propose_fix`, and every `faultforge_get_run` summary, carries `gate_trust`:

- `"local"`: every row came from this machine's sandbox.
- `"remote-reported"`: the run was made on the runner VM. Of the gate's rows, **two are verified on this machine**: `app_policy` (the candidate's `app.py` is read here before anything is sent) and `code_changed` (the host recomputes the hash of the code it sent, or of the source a build must report, and refuses an answer that differs). **The rest are what the VM's sandbox reported**: the verdict and its evidence (`comparison_fix_verified`), the direct database read (`state_verification`) and every regression run (`regression_suite`), and the containment facts. The host checks their shape (a valid `SandboxResult`, a report whose verdict its evidence supports, only the files a run writes) but not that the load really ran as reported. **A sandbox escape on the VM**, or anyone else with `runner` on it, **could forge those rows.**

The owner chose to trust remote verdicts, labelled and hardened, without a local confirmation run (2026-11-16). So a `verified: true` with `gate_trust: "remote-reported"` means "verified on the runner VM". To have it judged here, make the baseline locally and verify locally.

**A fix is judged on its baseline's runtime.** `faultforge_verify_fix(runtime="auto")` uses the runtime the baseline ran on, and an explicit other runtime is `not_comparable`, so both sides of a comparison come from the same machine. A remote baseline therefore needs a usable remote runtime to be verified (or a new local baseline).

`source_left_this_machine` is on every result of a call that chose a runtime, errors included: `true` as soon as any step's request (so the snapshot) was handed to ssh, even if the step then failed, and `false` when nothing was sent.

### Configure

Add this to `~/.config/faultforge/mcp.toml` (`user`, the two paths and `hourly_usd` are the defaults shown):

```toml
[remote]
host = "137.220.54.116"
user = "runner"
key_path = "~/.ssh/faultforge_vultr"
known_hosts_path = "~/.config/faultforge/runner_known_hosts"
hourly_usd = 0.066
# auto = "local"   # keep runtime "auto" on this machine; "remote" (the default) prefers the VM when allowed
```

Without the section the remote runtime is unavailable with the reason `no [remote] section in mcp.toml`. A bad section (an unknown key, `user = "root"`, a relative or odd path, a rate outside (0, 10]) disables the remote runtime only, with the reason; the rest of the file still applies.

### Deploy

```bash
scripts/deploy_runner.sh HEAD          # a commit
scripts/deploy_runner.sh --worktree    # uncommitted work, as a git tree from a temporary index: no commit, no ref
```

The owner (or an orchestrating agent) runs it; the server never does, and never deploys on its own. It reads the host, user and known_hosts from `[remote]`, logs in with the deploy key (`FF_RUNNER_DEPLOY_KEY`, default `~/.ssh/faultforge_vultr`) and uses the pinned host key only. In order:

0. Every path the tree would hold is checked with `snapshot.is_secret`, the rule a project snapshot uses (any path component: `.env*`, `*.pem`, `*.key`, `id_rsa*`, `.npmrc`, ...), and the deploy refuses before git writes anything. `--worktree` lists tracked and untracked, non-ignored files first; a commit lists its tree.
1. `git archive` of the tree, unpacked to `/home/runner/faultforge/app/<commit or tree id>`.
2. FaultForge's wheel, built **on this machine** from the same tree (`uv build --wheel`) and uploaded, so no build backend (hatchling) or sdist is ever fetched on the VM.
3. uv **0.12.18**, from the release tarball pinned in `uv-release.lock` (its sha256 matches the one published beside the release and is checked on the VM before unpacking); then `uv sync --frozen --no-dev --no-install-project --no-build` with the VM's own `/usr/bin/python3` (`UV_PYTHON_DOWNLOADS=never`): the dependencies (httpx, pydantic, PyYAML and theirs) as wheels only, from PyPI, each checked against the hash in `uv.lock`; then the wheel with `uv pip install --no-deps --no-index --no-build`, offline. The VM's code hash is computed over the installed package; a local check showed the wheel's files hash the same as `src/faultforge`.
4. `bin/ff-entry`, the forced command for a restricted server key (below), and a cron line for `runner` that every 15 minutes removes request directories older than two hours, by the fixed path and the 16-hex-character names only (`find /home/runner/faultforge/snapshots -mindepth 1 -maxdepth 1 -type d -regex '.*/[0-9a-f]{16}' -mmin +120`).
5. The images (`faultforge-target`, `-runner`, `-stack-hooks`, `-egress-proxy`, every local `faultforge-node-build`/`-run` toolchain, and `postgres:16-alpine` and `redis:7-alpine` if present; `FF_RUNNER_STOCK_IMAGES` changes the last two) by `docker save | gzip | ssh docker load`, only those whose ID differs, then every ID compared. Nothing is built or pulled on the VM.
6. The deploy record, `<state>/remote/deployed.json` (mode 0600), from what the VM itself reports: its code hash and image IDs.
7. Older app directories on the VM are removed.

Run `scripts/build_images.sh` first: the runner image holds FaultForge's source, so any change under `src/` changes its ID and needs a new deploy. Measured: the first deploy took 79 s (55 s for the images); see docs/VERIFIED.md.

### When remote is used

`runtime` is a parameter of `faultforge_run_scenario` and `faultforge_verify_fix`, default `auto`.

- `local` always runs here.
- `remote` requires every one of these, checked cheapest first, and fails closed with the first that doesn't hold: the `[remote]` section; a deploy record for that host and user that holds the four sandbox images; this machine's FaultForge code hash (every file of the package but bytecode) and image IDs equal to the record's; the key and known_hosts files; the credit floor for this call's worst case (`credit_floor`, see below); and one SSH call in which the VM reports its own code hash and image IDs, which must equal this machine's. Otherwise the error is `runtime_unavailable`, with `scripts/deploy_runner.sh` as the hint for a mismatch. Nothing is snapshotted or sent before these pass.
- `auto` uses remote only when all of them hold and `[remote] auto` isn't `"local"`, and local otherwise; `runtime_reason` says which and why (`auto resolves to local: remote: no billing key`, `... the deployed FaultForge (37d3087a40e2) isn't the code on this machine`, ...). The owner's default, with the section added, is to prefer the VM whenever it is allowed: that is the owner's stated choice (docs/PLAN.md V4.2), and `auto = "local"` turns it off.
- A stack run of a prebuilt `image` always runs here: the app image is on this machine only. Explicit `remote` gives `runtime_unavailable`.
- **`vultr` fixes run their candidates here.** The model call and the inference key must stay on this machine, and the vultr loop runs up to 3 candidates, each with its regression suite, through one callback; M12 keeps it local rather than add a second paid resource to the loop's worst case. The result says `runtime: "local"` and why.
- `faultforge_status` shows `runtimes.remote`: `available`, `reason` (the first failing condition, or the credit line), `host`, `hourly_usd`, `auto`, `deployed` and `what_leaves`, and `effective_modes.runtime` says what `auto` resolves to. It makes the same SSH ping, with a 25 s limit.

**The worst case** (docs/PLAN.md V4.3) is `hourly_usd` times the call's wall limit, rounded up to whole hours. The wall limit is the sum of every step the call can make, each at its hard deadline, plus a ping: a Python run is 645 s (one hour, $0.066); a stack run from source, build included, 3 015 s (one hour); a Python `verify_fix` with 3 regression scenarios 2 505 s (one hour); a stack `verify_fix` with 3, 5 025 s (two hours, $0.132); a Python `verify_fix` with 20, 13 045 s (four hours, $0.264). `CreditGuard.decide` judges it against the $10 floor exactly as a vultr fix, with the ledger's caps asked first. The VM bills by the hour whether it is used or not; the floor is what keeps FaultForge from choosing to rely on it below $10.

- `faultforge_verify_fix`: `auto` is the baseline's runtime; see "What the gate trusts".

### SSH and the VM

- Every call is `ssh -F /dev/null` with `BatchMode=yes`, `StrictHostKeyChecking=yes`, `UserKnownHostsFile=<known_hosts_path>`, no global known hosts, `IdentitiesOnly=yes`, `IdentityAgent=none`, no forwarding, no TTY, `ConnectTimeout=10` and keepalives, as `runner`, never root. `ssh` itself is resolved against the fixed system PATH (`/usr/local/bin:/usr/bin:/bin`), never the caller's, and must be an absolute path.
- The remote command is always `/home/runner/faultforge/app/<id>/.venv/bin/python -I -m faultforge.remote_run`, where `<id>` is the record's hex commit or tree id, checked against a strict pattern. A stack call also needs its own toolchain, postgres and redis images in the record, so they are compared too; otherwise `auto` stays local. No argument follows. The request is one JSON line on stdin, then a tar of the step's inputs; the answer is one JSON line on stdout, then a tar of the run directory. No project text is on any command line.
- Paths on the VM are fixed: each request gets a new random directory under `/home/runner/faultforge/snapshots/`, which is removed in a `finally` before the answer is written, whatever happened, and the answer says so (`snapshot_removed`); the host refuses an answer that doesn't. The install cache for stacks lives on the VM (`/home/runner/faultforge/state`, Docker volumes `ff-deps-*`), filled through the VM's own egress proxy under the same `allow_network_install` setting.
- Each request carries the code hash and image IDs the host expects, and the VM refuses (`deploy_mismatch`) before it unpacks anything if its own differ.
- **Deadlines.** The VM gives each step its local hard limit (a Python run 500 s, the sandbox's wall limit plus its removal margin, so its containers' expiry labels have passed by then) and the host's `ssh` gets that plus 120 s for the transfer, after which its process group is killed. Output above 48 MB plus the run directory's cap kills it too.
- **What comes back is checked like a build's output.** Only the directory of the run the answer names comes back (`.faultforge/runs/<report.run_id>/`), and inside it only the names a run writes (`report.json`, `report.md`, `requests.jsonl`, `trials/<n>/` with `k6-summary.json`, `k6.log`, `state-before.json`, `state-after.json`). Anything else (a planted `pinned/`, `run.json`, a second run, a link of either kind, a device or FIFO, an absolute path or `..`) refuses the whole archive before any of it reaches the run directory, so no orphan run is stored. At most 96 MB and 20 000 members, each checked from its header. The result must be a valid `SandboxResult`, so a report whose verdict its evidence doesn't support is refused as it is locally. A Python run must report the code hash of the files sent and a build the hash of the source sent. Any failure is `sandbox_failed` (`the remote runtime failed: ...`) and nothing from that call is stored; in `verify_fix` a failed remote step fails the whole call rather than one row. Each step sends only the baseline's `report.json`, not the call's earlier runs.
- **The reaper.** At the start of every request, pings included, the VM removes request directories older than two hours (only names it makes); requests that act also remove FaultForge's labelled containers and networks past their expiry. The cron line removes the same directories every 15 minutes, so a session dropped mid-run (its process killed before its `finally`) leaves nothing for long. Built images with the candidate label older than two hours are swept by the build step, as locally, so a long `verify_fix` never loses the image it built. It never prunes.

### Restricting the server's key (prepared, not applied)

Today the server's `key_path` is `~/.ssh/faultforge_vultr`, the key `scripts/provision_runner.sh` copied from root to `runner`: **one key logs in as root and as an unrestricted runner** (the owner's choice at provisioning). The prepared change gives the server a key of its own that can run nothing but the entry point:

1. `ssh-keygen -t ed25519 -N "" -C faultforge-mcp -f ~/.ssh/faultforge_runner_mcp`
2. `scripts/restrict_runner_key.sh ~/.ssh/faultforge_runner_mcp.pub` prints the line; with `--apply` it appends it, as runner over the deploy key, to `/home/runner/.ssh/authorized_keys`, after checking that `bin/ff-entry` is installed. Nothing else on the VM changes:

   ```
   restrict,command="/home/runner/faultforge/bin/ff-entry" ssh-ed25519 <the new public key> faultforge-mcp
   ```
3. Set `key_path = "~/.ssh/faultforge_runner_mcp"` in `[remote]`. `deploy_runner.sh` keeps using the deploy key (`FF_RUNNER_DEPLOY_KEY`), so the deploy needs no fixed verbs of its own.

`restrict` turns off forwarding, the agent, X11 and the TTY. `ff-entry` (`scripts/runner_entry.sh`) matches `SSH_ORIGINAL_COMMAND` whole against `^/home/runner/faultforge/app/<hex id>/.venv/bin/python -I -m faultforge.remote_run$`, never hands it to a shell, and execs only that; anything else gets a refusal in the entry point's answer format. The deploy record helper only pings through the entry point, so it works with the restricted key. What it doesn't do: a process that already runs as `runner` (a sandbox escape) can edit `authorized_keys` and `ff-entry`; the restriction limits a stolen server key, not a compromised VM. Removing root's use of the shared key is a separate owner decision.

### Rebuilding the runner VM

The VM holds no secrets, but every remote verdict is only as good as its integrity. Rebuild it from scratch (reinstall the OS on the same instance in the Vultr console, or replace the instance; either needs the owner, and a new instance needs its price shown first), then:

1. pin the new host key in `~/.config/faultforge/runner_known_hosts` (`ssh-keyscan` once, compared with the console's fingerprint);
2. `scripts/provision_runner.sh <ip>`;
3. `scripts/build_images.sh`, then `scripts/deploy_runner.sh HEAD`;
4. re-apply the key restriction if it was in use.

**When:** monthly as routine; at once after any sign of tampering (an answer refused for a hash or deploy mismatch the owner didn't cause, an unexpected file or process under `/home/runner`, a `remote-reported` verdict that a local run contradicts); after a Docker or kernel advisory affecting container isolation; and before relying on a remote verdict for something that matters (or verify that one locally instead).

### Limits, left as they are

- **Remote rows are reported, not proven.** The host checks the shape of everything, the hashes of what it sent and which files come back, and writes the stored `report.json` and `report.md` from the validated answer, never the archive's copy. It can't check that the load ran as reported: see "What the gate trusts". It is FaultForge's own VM, keys-only SSH, with SSH as the only open port.
- A killed `ssh` doesn't always stop the VM's process at once; the VM's own deadline and the reaper do. If that deadline lands during a container removal, the removal stops and the containers wait for the reaper's expiry rule.
- `faultforge_status` pings the VM and reads the credit whenever `[remote]` is present, even with `auto = "local"` (up to 25 s for the ping).
- The ledger's caps are asked for a remote run's worst case but nothing is recorded in it: the VM's cost is its hourly bill, not FaultForge's spend.
- One VM, no queue: two servers with different state directories can use it at once, each in its own request directories.

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
- **The sandbox.** The sandbox policy is the same as the CLI's and the web app's (docs/PLAN.md V3.3): no network, a read-only root, non-root, all capabilities dropped and hard limits. Only containers and networks labelled `faultforge.sandbox` are touched, and only cache volumes labelled `faultforge.depcache`, each by a name or ID FaultForge computed. The server never prunes anything.
- **A project's `stack.yaml` is untrusted here.**
  - The trusted services must be stock `postgres:<tag>` (version 16 or newer) and, if named, `redis:<tag>` images.
  - `target.hooks_origin` must be the hooks helper's own port, so the runner's token never goes to the app.
  - The `build:` section takes versions, plain names and a `node` command only: no image name and no build command.
  - The `prestart:` section takes a `node` command of plain tokens, a plain role name, a variable name the sandbox doesn't set itself, and a limit of at most 60 s.
  - The app may not run as root.
  - Writable paths must be plain paths.
- **One run at a time.** A per-process lock and an `flock` on `<state>/run.lock` are both held for a run and for a `verify_fix`, so a second call, in this server or in another server process sharing the state directory, gets `busy`, while `status` (which reports `busy`), `get_run` and a client-mode `propose_fix` still answer. A client-mode `propose_fix` takes no lock: it sends no load and only reads a stored run, whose files are complete before its `run_id` is returned. A vultr fix holds both locks like a run, so `auto` returns `busy` when it would have picked `vultr` during a run. The kernel releases the file lock when the server exits, however it exits.
- **Runs** are stored in `<state>/runs/<run_id>/`: `report.json`, `requests.jsonl`, the trial files, `run.json`, `sandbox.json`, `pinned/` (with `app.py` for a Python baseline) and, after a vultr fix, `candidates/`. A `run_id` is checked against its strict format before it's used in a path. `compare_to` must be an earlier run of the same scenario in the same project.
- **Results stay short,** at about 60 lines. Text a target could influence, such as captured response fields and probe results, is cleaned, cut short and labelled.
- **The hostile demo's opt-in is never set.** The server never sets `FF_HOSTILE_ALLOWED`, so `demo/hostile-build` refuses its attacks and doesn't show them off. Run that demo through the web app or the tests.

### Known limits, left as they are

- **Typed arguments over stdio.** An argument of the wrong type (a number for `project_dir`, say) is rejected by the MCP SDK's pydantic validation before the tool runs, so that one failure arrives as a protocol error rather than a `{status: "error"}` result. Every argument that reaches a tool is checked by the tool itself.
- **The stack's pinned SQL runs with the table owner's rights.** The state queries and the reset run as `faultforge_hooks`, an ordinary role that is a member of the role owning the tables, inside a throwaway container with no network, a read-only root and hard limits. They can read and change anything that owner can in that container's database, and nothing outside it. The superuser sets up the roles and never runs project SQL.
- **The review flag lists some kinds of object, not every way to change a read.** See "What it doesn't see" under "The review flag".
- **The baseline is whichever run the caller names.** Pinning stops a fix from changing the rules and the hooks' files of the run it is judged against. It doesn't stop someone from making a new baseline from a tree whose config or package is already weak, and verifying against that. The verify result names the `baseline_run_id` and the pinned config's sha256, so check them against the run that proved the bug.
- **The state directory is trusted.** The pinned copies and their hash live side by side in `<state>/runs/`, which is private to the user. The hash catches a damaged copy, not someone who can write there.
- **The per-call cap bounds what is sent, not what a provider reports.** A call is refused unless its worst case is within $0.10, and the ledger records what the reply's usage says. A provider that reported more output than the limit it was given would be charged to the ledger in full, and the next decision would see it. The owner accepted this: the $10 buffer is what it's for.
- **The ledger is only as accurate as its price table.** Costs are computed from token counts and the hardcoded `AGENT_PRICES` ($0.75 and $3.00 per million for `glm-5.3`). If Vultr changes its prices, the ledger and the caps drift until the table is updated.
- **A run holds at most 64 candidates.** A vultr fix is refused, before any model call, when its 3 attempts might not fit.
- **Credit after a fix is an estimate:** the last account reading less the ledger's spend since. Vultr's own number catches up at the next reading.
- **The key files are trusted to be yours.** The checks stop a link, a shared mode and another owner. They don't stop someone who can already write files as you.
- **`faultforge_run_scenario` with `compare_to` is not the gate.** It uses the snapshot's own config, as in M8. A changed scenario makes it `NOT_COMPARABLE`, but it doesn't run the regression suite or pin the queries. Use `faultforge_verify_fix` to judge a fix.

## Not exposed

- Arbitrary URLs, arbitrary scripts, load overrides and cancel.
- Any write to the user's repo.
- Applying a vultr diff: FaultForge never does it.
- The `vultr` fix mode for anything but a `ticket_support` project, and so for every stack, and a `vultr` fix's candidates on the remote runtime.
- Deploying from the server: only `scripts/deploy_runner.sh` deploys, run by hand.
- Install scripts, pnpmfiles, workspaces and any registry but `registry.npmjs.org` for a stack built from source.
- A shell, an image or a user of its own for the `prestart` step: it is a `node` command in the app's image, as the app's user.
- Build-time env values for a stack: recorded for JADE in docs/PLAN.md.
