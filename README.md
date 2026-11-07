# FaultForge

FaultForge runs real concurrent load against a web service in a disposable sandbox and checks business rules, not just status codes. When a rule breaks, an AI agent writes a fix, and the same load decides whether the fix holds.

> Every request returned success, and the app was still wrong.

Entry for the Vultr Agent Rush hackathon, Blast Radius Zero track.

## Live demo

**https://155-138-147-248.sslip.io/**

1. Leave **Ticket API** selected and click **Run**. In about 2 s the page shows `FAIL`: 20 of 20 requests accepted for 1 ticket. See **What the app said vs. what happened**, then **Proven** and **Suspected · not proven**.
2. Click **Let the agent fix it**. The fix is written live and tested in new sandboxes. Allow up to a minute. Read the **Fix verified** badge and its evidence rows.
3. Click **Start a new run**, choose **Hostile build** and click **Run**. Each attack is listed with its result and what the host verified. The verdict is `INCONCLUSIVE`, never `PASS`.

Visitors can only choose fixed targets. There are no uploads and no free-text prompts. The limits are a 30 s cooldown between runs, 20 runs per hour per visitor, and 3 fixes per hour and 6 per day per visitor. Recorded runs under **Past runs** always carry a Replay label.

## Vultr services and infrastructure used

- **Vultr Cloud Compute:** one VM, 2 vCPU and 4 GB, in Toronto. It hosts the web app, the agent loop and every sandbox.
- **Vultr Serverless Inference:** every LLM call. The live agent uses `glm-5.3` at reasoning effort `low`.

## Backend and deployment architecture

```
Browser
   │ HTTPS
   ▼
Vultr Cloud Compute VM (firewall: 22, 80, 443 only)
┌──────────────────────────────────────────────────────┐
│ Caddy ──▶ Control plane, non-root, 127.0.0.1:8080    │     Vultr Serverless
│           FastAPI, static page, agent loop,          │     Inference
│           sandbox manager, inference key             │ ──▶ report + app.py in,
│              │ docker CLI, argument lists            │     one candidate file out;
│              ▼                                       │     the model has no tools
│  New sandbox pair for every run                      │
│  ┌──────── shared loopback only ───────┐             │
│  │ target (untrusted)                  │             │
│  │ network none, no secrets            │             │
│  │        ▲ 127.0.0.1                  │             │
│  │        ▼                            │             │
│  │ runner: k6 + FaultForge             │             │
│  │ writes the report to /out           │             │
│  └─────────────────────────────────────┘             │
└──────────────────────────────────────────────────────┘
```

Caddy terminates HTTPS and forwards to the app, which runs under systemd. The target starts with `--network none`, and the runner joins that network namespace, so the only thing either can reach is the other on loopback. Both containers are removed after each run. Spending caps are $2 a day and $15 in total.

## The agent's use case

Concurrency bugs often pass unit tests and return clean responses. The agent turns a proven failure into a tested fix:

1. Load runs against the buggy target and produces a `FAIL` report.
2. The report and `app.py`, marked as untrusted data, go to Vultr Serverless Inference with the business rules.
3. The model returns one complete replacement for `app.py`. Code rules check it on the host without executing it.
4. The file runs in a new sandbox under the same workload.
5. A candidate that passes then runs every other scenario defined for the target, to catch what the fix broke.
6. If the fix gate doesn't pass, the result goes back to the model, for up to 3 attempts. After that the page reports failure.

FaultForge's checks decide success, never the model.

## Containment model

| Control | How it's enforced | How it's verified |
|---|---|---|
| Network | Target `--network none`; runner `--network container:<target>` | On the VM: network none on the target; external connection and DNS attacks blocked |
| Filesystem | `--read-only`, `/tmp` tmpfs (`noexec,nosuid,nodev`), `--ipc none` | On the VM: read-only root; writes outside `/tmp` blocked |
| Privileges | Non-root users, `--cap-drop ALL`, `no-new-privileges` | On the VM: non-root; capabilities all dropped |
| AppArmor | Docker's `docker-default` profile | On the VM: present on both containers |
| Secrets | Allowlisted environment, no Docker socket, key stays in the control plane | On the VM: no keys found; no Docker socket |
| Memory | Target 256 MB, runner 512 MB, swap equal to memory | On the VM: attack killed at the 256 MB limit |
| Processes | Target 128 pids, runner 512 pids | On the VM: attack stopped by the 128 limit |
| CPU | Target 1 CPU, runner 2 CPUs | On the VM: limited to 1 CPU, **not blocked**. The attack stops itself after 10 s; code that doesn't stop is bounded only by the time limit |
| Time | 100 s host wall limit per sandbox | `tests/test_sandbox.py`, with a 4 s limit: killed, no containers left |
| Leftovers | Labelled containers, removal after each run, and a reaper | On the VM: 0 containers left after the hostile build |
| Host | Firewall, non-root app, loopback bind, HTTPS | On the VM: only 22, 80 and 443 allowed; app runs as non-root; HTTPS |

## How a verdict is reached

A status code says what the service claimed. In the demo, 20 buyers race for 1 ticket, all 20 get a success response, and stock reads 0, so the app looks healthy. The evidence is the 20 accepted requests set against the 1 ticket that existed.

The runner resets the target, sends the load and compares client-measured counts with state before and after each trial. The scenario in `demo/ticket-api/faultforge.yaml` has seven invariants, such as `client.accepted <= before.stock` and `after.payments_unattached == 0`.

`FAIL` means at least one violation was proven. `PASS` means all trials completed, every invariant could be evaluated and no violation was proven. `INCONCLUSIVE` means no proven violation but incomplete evidence, which isn't a pass. `ERROR` means no trial started.

**Violations and suspected causes are separate.** A violation is measured and sets the verdict. A suspected cause, such as a lost update, is a hint about where to look. It comes from fixed rules, and never affects the verdict. Latency doesn't affect the verdict either.

**The fix gate needs all four conditions:**

1. **The comparison reports `fix_verified`.** The candidate's verdict is `PASS`, at least one invariant is `FIXED`, and none is `NOT_VERIFIED`, `STILL_FAILING` or `REGRESSED`.
2. **The direct database check matches.** The host reads the target's database with fixed read-only queries and compares the result with what the target reported for the final trial.
3. **The deployed code changed.** The host hashes the files it wrote for the target, and the hash must differ from the baseline.
4. **Every other scenario still passes.** The same code runs in a new sandbox for each other scenario defined for the target. Anything but a clean `PASS` with a matching database blocks the fix, and the result goes back to the model.

The second condition exists because a report alone can be forged: one forged fix got `PASS` and `fix_verified` true, reporting 0 orphaned holds while the database had 19.

The fourth exists because a fix can create a new bug. A fix that sets stock to 0 instead of subtracting 1 passes the race, because with 1 ticket in stock the two look the same. `plenty-of-stock`, with 20 tickets for 20 buyers, catches it with 1 accepted. This finds only what the defined scenarios check, so a verified fix doesn't mean the app has no other bugs.

## Measured results

| Test | Result |
|---|---|
| Ticket API on the VM | 20 of 20 accepted for 1 ticket; stock reads 0; `FAIL` in 3/3 trials; about 2 s a run |
| Live agent on the public site, `glm-5.3@low`, before regression checks existed | The first 3 live fixes passed the three-condition gate on attempt 1, at $0.005 to $0.009 each. Two were timed: 8.5 s and 15.5 s |
| Forged fix | Fooled the report alone; the database check caught it (reported 0 orphaned holds, database had 19) |
| Hostile build on the VM, 9 attacks | 8 blocked, absent or stopped by a limit; CPU limited, not blocked. `INCONCLUSIVE`, server healthy, 0 containers left |
| Tests and review | 960 tests pass. Independent reviews found a blocker and serious issues, which were fixed |

**Model trial** on a dev machine, before regression checks existed, fixes that passed the three-condition gate out of 10 tries each, $0.12 for all 60: `glm-5.3@low` 10; `mimo-v2.6-pro@none` 8; `deepseek-v4.1-flash-1017@none` 8; `glm-5.3-flash@low` 7; `qwen3.8-flash-next@none` 6; `qwen3.8-27b@none` 5.

[docs/VERIFIED.md](docs/VERIFIED.md) records how tools and limits behaved when tested.

## Honest limits

- **Containers, not VMs.** Sandboxes share the VM's kernel. gVisor is not in use.
- **The app's user can control Docker,** which is root-equivalent on the VM. That's why the app is alone there and takes no free-form input.
- **The payment provider is a stub:** in-process, with a fixed 50 ms delay, which widens the race window.
- **State comes from the target's own hook.** The direct database check covers the final trial only, and hostile code could edit its own database, which the check would then agree with.
- **The AST code rules are defence in depth,** not proof.
- **`fix_verified` is narrow.** It means the invariants held for this workload, these trials and this code, not that the race can never happen.
- **Fixed targets only.** Visitors can't upload code, so this demo doesn't test arbitrary applications.
- **FaultForge doesn't judge design quality.** In the model trial, some passing fixes from other models used an in-process lock, which wouldn't hold across workers.

## Setup

Needs Linux on x86-64 (the k6 installer supports only that), [uv](https://docs.astral.sh/uv/), Python 3.11 or later, and Docker.

```bash
uv sync                          # install dependencies
bash scripts/install_k6.sh       # pinned k6, checksum verified, to .tools/bin/k6
bash scripts/build_images.sh     # faultforge-target:dev and faultforge-runner:dev
uv run python -m faultforge.web  # http://127.0.0.1:8080
uv run pytest                    # everything, including real containers
uv run pytest -m "not docker"    # without Docker
uv run faultforge --project demo/ticket-api list    # scenarios and limits; sends no load
uv run python -m faultforge.schema                  # regenerate the report schema
```

The agent is off unless both `FF_AGENT_ENABLED=1` and `VULTR_INFERENCE_API_KEY` are set. Load tests and the hostile build work without them.

**Deploy** to a new Ubuntu 24.04 VM, with its address in `IP`:

```bash
scp deploy/provision.sh root@$IP:/root/provision.sh
ssh root@$IP bash /root/provision.sh
deploy/deploy.sh $IP
```

The site is then at `https://<ip-with-dashes>.sslip.io/`. [deploy/README.md](deploy/README.md) covers enabling the agent and the checks to run on the VM.

## Repo layout

```
src/faultforge/        Core: config, safety, k6 runner, oracle, report, sandbox, fix_agent, CLI
src/faultforge/web/    FastAPI app, run manager, budget, replays, static page
demo/ticket-api/       Buggy target, reference fix, false fixes, business rules
demo/hostile-build/    Containment target; don't run it outside a limited container
demo/sportsdeck/       A multi-service target, built from a Git repo at a pinned commit
deploy/                Provisioning, deployment, systemd unit, Caddy template
scripts/               k6 install, image build, model trial
docs/                  Plan, verified tool behaviour
```

This project was built with AI coding assistance.

## License

[MIT](LICENSE). The self-hosted fonts under `src/faultforge/web/static/fonts/` keep their own OFL licenses.
