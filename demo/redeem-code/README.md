# Redeem-code demo

A small SvelteKit and Postgres service with one real concurrency bug, built from source by
FaultForge's Node builder. It uses the same toolchain as a real project it was shaped after: Node
24.22.3, pnpm 12.21.0, SvelteKit 3 with adapter-node, Vite 8 and `pg`.

`POST /api/redeem {code, user}` redeems a code: 200 for the winner, 409 if it was already redeemed,
410 if it was revoked, 404 if it doesn't exist. `src/lib/server/redeem.ts` reads the code's status,
loads the kit's 40 items one query at a time (an ordinary N+1), then marks the code redeemed in a
transaction that never checks the status again. Two requests can both read `unused`, so one code is
redeemed many times. There is no sleep: the window is the real round trips to Postgres.

## Migrations and roles

Migrations are a step of their own, as in the project this demo is shaped after. `stack.yaml` has a
`prestart:` section: `node tools/migrate.ts` runs once in the app image before the app, as
`redeem_owner`, which owns the database. It takes an advisory lock, checks that the app role exists
and can log in, and applies each pending file of `migrations/` in its own transaction with a row in
`schema_migrations`. The app logs in as `redeem_app`, which owns nothing: `0000_init.sql` grants it
what it needs by name, and it can't delete an audit row or alter a table. The owner's URL is in
`MIGRATION_DATABASE_URL`, for that step only; the app gets `DATABASE_URL`. If the step fails, the
stack doesn't start. `tools/` is outside the fix scope, so a fix can't change the runner;
`migrations/` is inside it, so a fix can add a migration.

## Run it

Through the MCP server (docs/MCP.md), with this directory in a git repo inside an allowed root:

```
faultforge_run_scenario(project_dir, "same-code-race", "worktree")     # builds from source, FAIL
faultforge_propose_fix(run_id)                                          # the brief
faultforge_verify_fix(run_id, "worktree")                               # after you edit src/ or migrations/
```

The first run of a lockfile needs `allow_network_install = true` in `mcp.toml`: the dependencies
are fetched once from registry.npmjs.org, through a proxy that reaches nothing else, and cached.

For the default test suite, which runs prebuilt images:

```bash
bash scripts/build_node_images.sh 24.22.3 12.21.0
bash scripts/build_redeem_demo.sh          # faultforge-redeem-code:buggy, :fixed and :fixed-schema
uv run pytest tests/test_redeem_code.py
```

## What is checked

`same-code-race` sends 20 requests for `RACE-1` at once. One may be accepted; a loser must get 409,
and a 404, 410 or 500 counts as an unexpected status, which is a violation by itself. The 409's
body isn't checked: FaultForge captures a field from accepted responses only. The database is read
by the host as a role the app can't log in as (`faultforge_hooks`, not a superuser), and must agree
with the hooks helper.

The other three scenarios are the regression suite. There are two correct fixes:

- `reference_fix/`, in `src/` only: one `UPDATE ... WHERE status = 'unused' RETURNING` claims the
  code, in a transaction with both inserts.
- `reference_fix_schema/`, a schema rule: `migrations/0001_one_redemption_per_code.sql` adds a
  unique constraint on `redemptions(code_id)`, and `src/lib/server/redeem.ts` answers the unique
  violation as 409. It only works because the prestart applies the migration.

`false_fixes/` holds fixes that look right and aren't, each with the gate row that rejects it.

`flagged_fix/` is the schema fix again, with a trigger in its migration that drops every audit row
whose action isn't `redeem`. Nothing the scenarios do writes any other action, so every gate row
passes and the fix is `verified`. FaultForge still compares the database's triggers, rules, views,
row-security policies, functions and the like with the baseline's, so the result says
`review_required: true` and names `trigger audit_log_only_redeems on public.audit_log` and the
function it calls. The gate's
answer is not changed by that: the flag is for a person to read the migration.

## What it can't catch

A migration is project code that runs as the database's owner. One written to fool the check, for
example by replacing `audit_log` with a view that always counts right, would not be caught by any
gate row (docs/MCP.md, "What a verified stack fix still doesn't prove"). The
`migration_mutes_audit` false fix is an honest-looking mistake of that kind, and is caught. Such a
view, or any trigger or rule a fix adds, is named in the result with `review_required: true`
(`flagged_fix/` shows it), but only a reader can tell a harmless one from a forgery. Read a fix's
migrations.

A fix that serialises redemptions with an in-process mutex passes, because the stack runs one Node
process. It would fail with two app instances, which this sandbox doesn't run.

`pnpm-lock.yaml` was generated with pnpm 12.21.0 from registry.npmjs.org only. The `node@runtime`
entry that `devEngines` needs was copied in by hand from another lockfile for the same Node release,
because generating it reads nodejs.org; pnpm accepts the result with `--frozen-lockfile`. FaultForge
never fetches that entry: Node comes from the toolchain image.
