# Verified tool behaviour

Tested on 2026-11-06 against k6 v2.3.0 (linux/amd64), the version pinned in `k6.lock`. `tests/test_k6_runner.py` covers the behaviour marked "tested".

| Assumption in the plan | Result | Consequence |
|---|---|---|
| `status 0 or error_code` identifies transport failures | **Wrong.** k6 also sets `error_code` on HTTP 4xx and 5xx, so 409s were classified as transport errors. | Only status 0 counts as a transport failure (tested). |
| `http.setResponseCallback(http.expectedStatuses(...))` stops 409s counting as failed | Correct: `http_req_failed` rate is 0 with 19 × 409 (tested). | Kept as a cross-check only. |
| `maxRedirects: 0` stops redirects being followed | Correct: the 302 is recorded and the redirect target gets 0 hits (tested). | Kept. |
| `blacklistIPs` blocks non-loopback addresses | The CLI flag is `--blacklist-ip` and it works. IPv4 and `::/0` are both enforced; loopback stays reachable (tested). | Added `::/0`, which the plan didn't include. |
| `--no-usage-report` exists | Correct. | Kept. |
| `--address` needs disabling | Not needed. The REST API is off unless `--address` is set. | Flag omitted. |
| `--system-tags` restricts tags | Accepted, and the summary holds no per-request values (tested). | Kept. |
| `handleSummary` writes aggregates to a file | Correct, including `iterations` and `dropped_iterations`. | Kept. A metric that never fired is absent, so missing counts are treated as 0. |
| k6 exits 0 when `maxDuration` cuts a run short | Correct: exit 0, with `dropped_iterations` set (tested). | A trial needs iteration and request counts, not just exit 0. |
| Sample time minus duration gives the request start | Not used. `timings.duration` excludes connection setup, and sample times use the local time zone. | The script records `Date.now()` before and after each request as tags, in whole milliseconds. |
| Default `gracefulStop` is suitable | No: it would let hung requests run 30 s past `maxDuration`. | Set to `0s`. Interrupted requests leave no sample, so the trial reads as `iterations_short` (tested). |
| Ambient `K6_*` settings could add outputs | Prevented by the minimal environment and `--include-system-env-vars=false` (tested). | `HOME` is a scratch directory, so `~/.config/k6/config.json` isn't read either. |
| `response.json(path)` captures a field | Correct. | Limited to 128 characters, on accepted responses only. |

## Python bytecode cache

Python validates a `.pyc` by source size and whole-second modification time. A same-size edit within the same second runs the **old code**: this reproduced in 3 of 3 direct tries, and in 1 of 12 runs of the fingerprint test. With `PYTHONDONTWRITEBYTECODE=1` the new code ran every time.

`build_id` was correct in these failures, because it reports what is actually executing. Target processes must set `PYTHONDONTWRITEBYTECODE=1` and start from a directory with no `__pycache__`, or a retest could run the code from before a fix.

## Unexplained: one connection reset across 4 workers

`test_reference_fix_holds_across_worker_processes` failed once with `httpx.ReadError` during the full suite on 2026-11-06. It was a reset, not a wrong sale. It didn't reproduce in 65 fresh 4-worker startups with 3 races each, 25 of them with all CPUs saturated, so the cause is unknown. The demo target and the sandbox run a single worker, so this doesn't affect the product path. The test allows one retry and prints the server log on a second reset.

## Behaviour inside the sandbox

Tested on 2026-11-06 with Docker Desktop 29.7.2 on WSL2 (cgroup v2), using `tests/test_sandbox.py` and the default `SandboxPolicy`: target 256 MB, 1 CPU, 128 pids; runner 512 MB, 2 CPUs, 512 pids. A real deployment on a VM uses a different Docker setup, so run these tests there too.

| Question | Result | Consequence |
|---|---|---|
| Does a memory kill yield a report or a structured failure? | Both occur, and neither is a traceback or a `PASS`. With 20 buffered 64 MB responses and nothing sampling the runner, k6 was OOM-killed and FaultForge survived in 20 of 20 runs: `engine_exit_nonzero`, `INCONCLUSIVE`, about 3.5 to 5.5 s. With `docker exec` sampling the cgroup every 0.1 s, the whole runner was killed in 3 of 6 runs with 4 trials (exit 137, no stdout), which gives `failed` with `runner_oom`. | Callers must handle both. Which one happens isn't controlled. The sampling probably changes the odds, but that wasn't isolated. |
| Does the runner stay within its memory limit? | Almost. `memory.peak` reached 514 to 521 MiB against a 512 MiB limit, with swap equal to memory. | It's an overshoot, not a breach of the limit's purpose: it's bounded in every run seen. The likely cause is the kernel force-charging tasks that are dying, which wasn't confirmed. The test allows 32 MiB. |
| What does `State.OOMKilled` mean? | It's true when any process in the container is OOM-killed, including when the main process survives and exits 2 or keeps running. | It doesn't mean the container died. `ContainerFacts` also reports `running_when_inspected` and `exit_code`, which is `None` while it's running. |
| Do the limits starve a run? | No. The buggy and fixed demo each take 4 to 5 s in total for 3 trials plus the serial control, and still race and pass as they do outside Docker. The runner peaked at 49 MiB on the demo, and at 62 to 64 MiB and 15 pids with 50 VUs and 500 requests. The peaks are sampled, so they're lower bounds. | The defaults stand. |
| Does `--read-only` break k6 or FaultForge? | No. k6's scratch directory is inside `/out`, `HOME` is `/tmp`, and SQLite's WAL works on the tmpfs. | No extra writable paths are needed. |
| Is `--read-only` enough to stop writes outside `/tmp`? | No. It leaves `/dev/shm` writable. | Both containers also use `--ipc none`, which removes it. Nothing needs it. |
| What happens when `--pids-limit` is hit? | Target: fork gets `BlockingIOError` after 125 children, and the hooks keep answering. Runner, 8 runs at each limit: 2 to 4 pids gave `failed` with `runner_error` (exit 3); 6 gave `INCONCLUSIVE` with `engine_exit_nonzero`; 8 gave 5 `INCONCLUSIVE` and 3 `PASS`. | A shortage that stops k6 can't produce `PASS`. A `PASS` at 8 means k6 fit and every request completed. The default of 512 is far above the 15 seen. |
| Does `--cpus` stop a spin? | No. It throttles to the limit (`nr_throttled` rises) and the spin ends by itself. The hostile build reports `cpu_spin` as `not_blocked`, which is accurate. | Only the host's wall limit bounds CPU use over time. Present this as limited, not blocked. |
| Can an image's environment be an allowlist? | Not with `FROM python:3.12-slim` alone, which adds `GPG_KEY` and others, and an image can't unset them. | The images copy their files into `FROM scratch`, so `docker inspect` shows only `PATH` and the names set per run. |
| Is `build_id` stable across sandboxes? | Yes, for identical source: 2 runs matched, with different `instance_id`s. A same-size edit changed it. | A retest of unchanged code reads as `build_unchanged`. This relies on the fixed `/app` mount path. |
| Does the host wall limit hold? | Yes, tested with a 4 s limit: the run was killed, `wall_limit_exceeded`, no containers left. | |

**Bridge gateway on Docker Desktop.** `172.17.0.1` exists inside Docker's VM rather than on this machine, so this machine can't bind it (`EADDRNOTAVAIL`). The egress test therefore uses a labelled helper container on Docker's host network to listen on the gateway, with loopback and primary-address listeners on this machine. A normal bridge container did reach the gateway listener, so the control is real. From both sandbox containers: gateway, primary address and `1.1.1.1` gave `ENETUNREACH`, DNS failed, and loopback was refused because it's the container's own. All listeners stayed at 0 hits.

## Model test on Vultr Serverless Inference (2026-11-06)

Each try sent the buggy `app.py`, the support code, the business rules and a real `FAIL` report, then ran the generated fix in a new sandbox. Success needs `fix_verified`, a matching direct database check and new code. One attempt per try, 10 tries per setting, $0.12 in total by the script's accounting.

| Model and reasoning setting | Passed | Median model seconds | Failures |
|---|---|---|---|
| `glm-5.3@low` | 10/10 | 23.4 | none |
| `mimo-v2.6-pro@none` | 8/10 | 1.7 | 2 code policy |
| `deepseek-v4.1-flash-1017@none` | 8/10 | 1.3 | 1 code policy, 1 unexpected 500 |
| `glm-5.3-flash@low` | 7/10 | 5.0 | 3 malformed replies (syntax, multiple blocks, truncated) |
| `qwen3.8-flash-next@none` | 6/10 | 3.0 | 4 malformed replies (3 multiple blocks, 1 truncated) |
| `qwen3.8-27b@none` | 5/10 | 2.2 | 4 multiple blocks, 1 still oversold |

- Without a reasoning setting, every model tried spent its 8,000-token allowance on reasoning and returned no content.
- Settings are model-specific. `glm-5.3` and `glm-5.3-flash` kept reasoning to the limit at `none` in a single test call, and `mimo-v2.6-pro` did at `low`.
- The verdict doesn't judge design quality: some passing fixes from other models used an in-process lock, which wouldn't hold across workers. All 10 `glm-5.3` fixes used the atomic claim.
- The demo default is `glm-5.3@low`, for reliability over speed. A first-attempt failure is unlikely with it, so a retry can't be scripted.

## On the Vultr VM (2026-11-06)

- `docker-default` AppArmor is applied to both containers, and `docker info` lists AppArmor. WSL2 couldn't test this.
- Network none on the target, read-only root, non-root users, and memory and process limits, read with `docker inspect` during real runs.
- Only ports 22, 80 and 443 are allowed. 8080, 2375 and 6565 don't respond from outside. SSH accepts keys only.
- Ticket API: `FAIL` with 20 of 20 accepted. Hostile build: `INCONCLUSIVE` in 26.5 s, with 0 containers left.
- The hostile CPU attack stops itself after 10 s. It's throttled, not blocked, so only the wall limit bounds code that doesn't stop.

## Regression scenarios (2026-11-07)

A candidate that passes the gate also runs `plenty-of-stock` (20 tickets, 20 buyers) and `sold-out` (0 tickets, 20 buyers), each in a new sandbox. Tested in real containers on the dev machine:

- The reference fix passes both, at about 7 s per scenario.
- `false_fixes/wipes_stock` sets stock to 0 instead of subtracting 1. It gets `PASS` and `fix_verified` on the race with a matching database, then `FAIL` on `plenty-of-stock` with 1 accepted.

Regression runs only check the scenarios defined in the config, so they can't find a bug in behaviour no scenario covers.

**With a real model:** `glm-5.3@low`, one attempt per try, 10 tries, $0.059. 9 passed the full gate, including both regression scenarios, in 21 to 28 s per try. The other returned more than one code block, so it never ran; a retry would have covered it in real use. No model fix was blocked by a regression scenario in this sample, so `wipes_stock` remains the only evidence that the check catches anything.

## SportsDeck spike (2026-11-07, local only)

Commit `065e475` of a Next.js, Postgres and Redis app, run on the dev machine in containers sharing one network namespace with no outside network, with random throwaway secrets and real k6 2.3.0. The app, Postgres and Redis were read-only with all capabilities dropped. FaultForge's own loop wasn't used, so this isn't a FaultForge verdict.

| Measure | Result |
|---|---|
| `POST /api/auth/refresh`, one token, 20 at once, 5 trials | Accepted 20, 3, 5, 3, 3, with that many live tokens left afterwards. No 500s. The 20 came from the first, cold trial |
| The same 20 requests one at a time | 1 accepted, 19 rejected, 1 token left |
| Hand-written one-file fix (atomic `deleteMany`, checks the count), 5 trials | 1 accepted, 19 rejected, 1 token left, in all 5 |
| Startup, including migrations | 11.5 s |
| Memory after load | App 114 MiB, Postgres 108 MiB, Redis 6 to 15 MiB |
| Image | 1.66 GB; first build 5 min 10 s; rebuild with dependencies cached 3 min 30 s |
| `next build` with 2 CPUs, 3 GB, no network | Worked: 140 s, 2214 MiB peak |

- The committed lockfile doesn't match `package.json`, so `npm ci` and the repo's own `docker build` fail. The spike used `npm install` in a scratch copy, so its dependency versions aren't pinned by the repo.
- The race is real but varies: after the first trial, overlap was 52 to 93 ms and 3 to 5 requests got through. A verdict needs "more than 1", not an exact count.
- A rebuild can't fit inside the 100 s sandbox limit, so a target like this needs a separate build phase with its own limit.

## SportsDeck through FaultForge's sealed stack (2026-11-07, local only)

`tests/test_stack.py`, with images from `demo/sportsdeck/build.sh` at commit `065e475`. Postgres owns a namespace with no network, and Redis, the app, a trusted hooks helper and the runner join it. All are read-only, with all capabilities dropped and non-root users.

| Run | Verdict | Detail |
|---|---|---|
| `refresh-token-race`, unfixed | `FAIL` | 20, 6 and 7 accepted across 3 trials; serial control 1 |
| The same, reference fix, compared with that run | `PASS` | Gate holds: comparison verified, direct `psql` check matched, image changed |
| `garbage-tokens-rejected`, reference fix | `PASS` | No token accepted, database matched |

Each run took about 26 s, with 0 containers left. The hooks helper can't count the app's in-flight requests, so state read at the end of a trial relies on the settle wait. The token and its signing secret are fixed test values.

## A real model fixing SportsDeck (2026-11-07, local only)

`scripts/stack_fix_trial.py`, `glm-5.3@low`, up to 3 attempts per try. The model saw the route, the read-only helpers with fallback secrets redacted, the business rules and the report, and returned one replacement file. Each candidate was compiled in a sealed container, then tested in a new stack.

First trial, before hardening, $0.0277: all 3 tries were verified, with 1 first attempt failing to compile and 1 breaking a code rule. Those prompts included the helpers' fallback secrets unredacted. Second trial, on the hardened path, $0.0277: all 3 tries were verified, with 1 first attempt breaking a code rule and 1 returning 2 code blocks.

- **The gate verified a bad fix.** Read by hand, 1 of those 6 verified fixes dropped the signature check and trusted any token found in the database. No scenario covered that, so it passed. `forged-signature-rejected` now covers it, using that model's own code as a false fix (`FAIL` on that scenario, in real containers), and the rule was added to the business rules. Other behaviour no scenario covers can still regress unseen.

Third trial, with that scenario, $0.0340: all 3 tries were verified. **In try 1 the regression step caught a real model fix:** attempt 1 passed the race, but it deleted the token before checking its signature, so a forged token consumed a stored row. `forged-signature-rejected` failed on "The stored token isn't consumed", that went back to the model, and attempt 2 checked the signature first and passed everything.

- A verified attempt took about 200 to 245 s: about 140 s to build, 4 to 18 s for the model, and the rest in the stacks. That's too slow to feel live on a page.
- All 9 tries across the three trials ended verified, and the model's reliability on a first attempt was lower: 4 of 9 first attempts passed the checks in place at the time. 9 tries is a small sample, and only this one route has been tried.
- The checks can only be as good as the scenarios, which is what the bad fix above shows.
- The route's `build` script sets its own `NODE_OPTIONS`, so the 2560 MB heap setting has no effect. The 3 GB container limit still applies.
- Everything ran on the dev machine. The 4 GB VM hasn't been tried.

**The recorded replays:** 5 tries, $0.0239, all kept in `demo/sportsdeck/replays`. All 5 were verified on the first attempt, with the signature rule now in the business rules, so these recordings don't show a regression catch. Unfixed, the first trial of each accepted 2 or 20 requests, and later trials 4 to 19. A verified attempt took about 230 s.

## Sealed builds (2026-11-07, local only)

`next build` does run AI-written code. A hostile route, with the rule check skipped on purpose, reported what it could reach from inside a real build: writes to `/app` and `/etc` failed, a connection to `1.1.1.1:443` failed, it ran as uid 1000, no secret-like variable names were present, there was no Docker socket, and `DATABASE_URL` held only the image's `localhost:5432` placeholder.

Build output is rebuilt on the host by a validator that refuses links out of the tree, hard links, devices and traversal, and drops setuid bits. These are tested with crafted archives. These sandboxes still share the host's kernel, and a kernel escape from a build isn't something these tests exercise.

## M11: the Node builder and the redeem-code demo (2026-11-16, local only)

Docker Desktop 29.7.2 on WSL2. Node 24.22.3, pnpm 12.21.0, SvelteKit 3.2.0, Vite 8.3.8. No Docker Hub pull was needed. The only network used was `registry.npmjs.org:443`.

### pnpm 12 (spike S1)

- **`pnpm fetch` has no `--no-runtime`.** It exits 2 with "unexpected argument". Without the flag it tries to download Node from nodejs.org, which the proxy refused. So the fetch step gets a copy of the lockfile with the `node@runtime` entry removed from the importer, the packages and the snapshots, written again from the parsed data. With that copy the proxy log named `registry.npmjs.org:443` and nothing else: 115 connections, 27 MB.
- **`pnpm fetch` writes more than the store.** It also lays out `node_modules/.pnpm` in the working directory, so it runs in the cache volume, not in a 16 MB tmpfs.
- **An offline install re-checks the lockfile against pnpm's supply-chain policies, and that needs registry metadata.** `pnpm install --offline --frozen-lockfile` failed with `ERR_PNPM_NO_OFFLINE_META` even with the metadata cache kept. The link step therefore passes `--trust-lockfile`. The fetch step, which has the network, still runs that check ("Lockfile passes supply-chain policies (97 entries)").
- **Generating a lockfile with `devEngines.runtime` reads nodejs.org** (`SHASUMS256.txt`). The proxy refused it. The demo's lockfile was generated without `devEngines`, and the `node@runtime:24.22.3` entry was copied in from another lockfile for the same release. pnpm accepts the result with `--frozen-lockfile --no-runtime`.
- **`--no-runtime` on `pnpm install` works:** no Node archive is fetched or linked.
- **An `--internal` Docker network still has a gateway on the host.** From a container on it, the gateway address answered (connection refused on closed ports). With `com.docker.network.bridge.gateway_mode_ipv4=isolated` there is no gateway address, and containers on the network still reach each other. The install network uses it.
- pnpm opens more connections than expected (up to 147 in one fetch). The proxy allows 128 at once and 256 pids.
- Cold, for the demo's 97 packages: fetch 6 to 9 s, link 2 s.

### The sealed build (spike S2)

- **As planned, it fails:** Vite bundles `vite.config.ts` into `node_modules/.vite-temp`, which is read-only.
- **`--configLoader native` isn't enough:** SvelteKit 3 also writes `node_modules/$app`.
- **`pnpm run` reinstalls first.** With a writable `node_modules`, pnpm 12 runs an install before the script, which emptied the directory and then failed offline. `PNPM_CONFIG_VERIFY_DEPS_BEFORE_RUN=false` turns that off.
- **What works:** the dependency volume mounted read-only at `/deps/node_modules`, and `/app/node_modules` a directory of links to it in the build's tmpfs. Generated files land in memory; a write to a package fails with "Read-only file system". The command stays the package's own `build` script.

| Tree | Wall | `memory.peak` | `/app` tmpfs at the end | `/tmp` |
|---|---|---|---|---|
| The demo (about 30 files) | 10.5 s | 339 MiB | 3.5 MB | 6.6 MB |
| Synthetic, JADE-sized: 690 files, 11 MB, 220 routes | 41.5 s | 1159 MiB | 65 MB | 6.7 MB |

The cap is 3072 MB, tmpfs included, so the larger tree peaked at 38% of it. The synthetic tree uses the demo's dependencies, not JADE's, so JADE's own peak is still unmeasured.

### The race, over 50 runs

`same-code-race` on the buggy image, through `run_stack`, 3 concurrent trials and a serial control each.

| | First cut (22 runs, stopped) | As shipped (50 runs) |
|---|---|---|
| Runs with verdict FAIL | 22 of 22 | 50 of 50 |
| Concurrent trials that accepted 1 | 16 | 0 |
| accepted 2 | 0 | 0 |
| accepted 3 to 5 | 9 | 0 |
| accepted 6 to 19 | 38 | 0 |
| accepted 20 | 3 | 150 |
| Serial control accepted 1 | 22 of 22 | 50 of 50 |
| Database matched | 22 of 22 | 50 of 50 |

- **The first cut's window was too narrow.** It read the status, read the kit and wrote, three round trips, under a millisecond. k6's 20 requests start 1 to 2 ms apart, so in about a quarter of the trials the first request finished alone. Every run still failed, because one of three trials was enough, but three such trials in one run would have passed: about 1 run in 50 by these numbers.
- **It was widened with real work, not a sleep.** The redeem now loads the kit's 40 items one query at a time, an ordinary N+1, and writes inside a transaction that doesn't check the status again. Every request reads `unused` before any writer commits.
- The 50 ran in two batches of 25, 17 to 31 s a run, with nothing else using Docker. An earlier batch of 17, run while another session was loading Docker, also failed 17 of 17 with 20 accepted in every trial; one of those runs reported the database check `unavailable`, which fails closed.
- The default test asserts only that some trial accepted more than 1.

### The whole pipeline, on the demo

| Step | Cold | Warm |
|---|---|---|
| Lint (and the source copy) | 0.6 to 1.8 s | same |
| Fetch, through the proxy | 8.7 s: 115 connections, 27 MB, all to `registry.npmjs.org:443` | none |
| Link, no network | 2.0 s | none |
| Sealed `vite build` | 10 to 21 s | same |
| Read and validate the output and the prod `node_modules` | 1.3 to 1.8 s | same |
| Image, no `RUN` | 1.9 to 4.7 s | same |
| One stack run | 16 to 20 s | same |

Through the MCP tools, with the real builder (`FF_TEST_STACK_BUILD=1`):

- `faultforge_run_scenario` from a worktree with a warm cache: 43 s, FAIL, database matched, limits applied, 0 containers left.
- `faultforge_verify_fix` of the reference fix, one build and 4 stack runs: 96 s, all four rows passed, 3 regression scenarios passed.
- Edits outside the fix scope alone: `code_changed` failed in under 10 s and no container was started.
- A syntax error in `src/`: `build_failed`, with the build's output marked `[project]`, and no stored run.
- Each false fix, built and run for real, was rejected by the row its README names: `rejects_everyone`, `skips_audit` and `wipes_codes` by `comparison_fix_verified`; `drops_revocation_check` by `regression_suite` (`revoked-code-refused`). No other row failed in any of them.
- A cold fetch through the tools' builder: 10.2 s, 115 connections, 29 MB, no refused host. From a container on the install network, `1.1.1.1`, the egress gateway (ports 443 and 3128), `example.com` and `registry.npmjs.org` directly were all unreachable. Afterwards no container or network of the run was left.

### Containment of a build

`vite.config.ts` with code that tries to get out, then fails the build with what it found:

- uid 1000; `1.1.1.1:443` and a registry address: `ENETUNREACH`;
- writes to `/etc`, `/src`, the dependency volume and a package under `/app/node_modules`: `EROFS`;
- no Docker socket; no variable named like a key, secret, token, password, database or proxy;
- a hash of every file's name, size and time in the cache volume was the same before and after.

It could start a child process (`id`), as any build can. The sandbox shares the host's kernel.

### Every stack: postgres, search_path, the token

- **Before M11 the app could log in as the administrator.** The stock postgres image trusts `127.0.0.1`, and the app shares that loopback. With `--auth-host=scram-sha-256`, from a container in the stack's namespace with the app's user and limits: `faultforge_admin` and `postgres`, with no password and with a wrong one, over `127.0.0.1` and `::1`, are all refused; the app's role with a wrong or no password is refused; the app's role with its password and the admin with its password (the helper's login, SCRAM through pg8000) succeed. `pg_hba_file_rules` shows `scram-sha-256` for every `host` rule. Tested for the demo's stack and SportsDeck's.
- **The search_path pin matters.** An app that owns its database can define `public.count(*)` and set its database's default `search_path` to `public, pg_catalog`. An unpinned admin session then got 999 for `COUNT(*)` over 3 rows. The helper's session and the host's `psql` both got 3.
- SportsDeck's real-stack tests pass with these changes.
- A stack's runner was reported as `limits_applied: false`: its facts were checked against the app's container, and it joins postgres's namespace. It is now checked against that. The limits themselves were always applied.

### Not done, or different from the plan

- **The runner and hooks images** were first patched with a Dockerfile that has no `RUN` and no network, because `scripts/build_images.sh` needs PyPI. Running `scripts/build_images.sh` afterwards against the same source was a full cache hit with the same image IDs, so the images in use are its own builds. The previous images are tagged `faultforge-runner:pre-m11` and `faultforge-stack-hooks:pre-m11`. A stack run now also checks that the hooks helper refuses a call without the run's token, so a stale helper image is refused, not trusted.
- JADE's own build wasn't run. Its `package.json`, lockfile, workspace file and `.nvmrc` pass the lint (321 packages).
- Nothing ran on the VM.

## Not tested

- The bridge-gateway egress test and the rest of `tests/test_sandbox.py` ran on the dev machine, not the VM.
- Race reliability on the VM over many runs: only a few runs so far.
- `MCP_TOOL_TIMEOUT`: not relevant while MCP is cut from the MVP.
