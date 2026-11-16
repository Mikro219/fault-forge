# Redeem-code demo

A small SvelteKit and Postgres service with one real concurrency bug, built from source by
FaultForge's Node builder. It uses the same toolchain as a real project it was shaped after: Node
24.22.3, pnpm 12.21.0, SvelteKit 3 with adapter-node, Vite 8 and `pg`.

`POST /api/redeem {code, user}` redeems a code: 200 for the winner, 409 if it was already redeemed,
410 if it was revoked, 404 if it doesn't exist. `src/lib/server/redeem.ts` reads the code's status,
loads the kit's 40 items one query at a time (an ordinary N+1), then marks the code redeemed in a
transaction that never checks the status again. Two requests can both read `unused`, so one code is
redeemed many times. There is no sleep: the window is the real round trips to Postgres.

## Run it

Through the MCP server (docs/MCP.md), with this directory in a git repo inside an allowed root:

```
faultforge_run_scenario(project_dir, "same-code-race", "worktree")     # builds from source, FAIL
faultforge_propose_fix(run_id)                                          # the brief
faultforge_verify_fix(run_id, "worktree")                               # after you edit src/
```

The first run of a lockfile needs `allow_network_install = true` in `mcp.toml`: the dependencies
are fetched once from registry.npmjs.org, through a proxy that reaches nothing else, and cached.

For the default test suite, which runs prebuilt images:

```bash
bash scripts/build_node_images.sh 24.22.3 12.21.0
bash scripts/build_redeem_demo.sh          # faultforge-redeem-code:buggy and :fixed
uv run pytest tests/test_redeem_code.py
```

## What is checked

`same-code-race` sends 20 requests for `RACE-1` at once. One may be accepted; a loser must get 409,
and a 404, 410 or 500 counts as an unexpected status, which is a violation by itself. The 409's
body isn't checked: FaultForge captures a field from accepted responses only. The database is read
by the host as an administrator the app can't log in as, and must agree with the hooks helper.

The other three scenarios are the regression suite. `reference_fix/` is a correct fix: one
`UPDATE ... WHERE status = 'unused' RETURNING` claims the code, in a transaction with both inserts.
`false_fixes/` holds fixes that look right and aren't, each with the gate row that rejects it.

## What it can't catch

A fix that serialises redemptions with an in-process mutex passes, because the stack runs one Node
process. It would fail with two app instances, which this sandbox doesn't run.

`pnpm-lock.yaml` was generated with pnpm 12.21.0 from registry.npmjs.org only. The `node@runtime`
entry that `devEngines` needs was copied in by hand from another lockfile for the same Node release,
because generating it reads nodejs.org; pnpm accepts the result with `--frozen-lockfile`. FaultForge
never fetches that entry: Node comes from the toolchain image.
