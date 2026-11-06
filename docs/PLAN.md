# FaultForge — Plan v3 (Vultr Agent Rush: Blast Radius Zero)

Status: **M0 done. v3 milestones awaiting approval.** Team: one person plus Claude Code.

This section **overrides** the v2.1 plan below wherever they conflict. The contracts (§4), verdict rules, budgets, comparison rules and demo target (M1) still apply. Claude Code and MCP are no longer the product interface.

## V3.1 Product

FaultForge is a public web agent that finds a concurrency bug by running real load, fixes it, and proves the fix. Every target, every load run and every line of AI-written code runs in a disposable sandbox on Vultr.

## V3.2 Architecture

```
Browser ──HTTPS──▶ Control plane on a Vultr VM (FastAPI + static page)
                    │  holds the inference key; never enters a sandbox
                    ├─▶ Agent ──▶ Vultr Serverless Inference (all LLM calls)
                    │     input: report.json + app source; output: one validated file
                    └─▶ Sandbox manager (docker CLI, argv only)
                          │  a new pair of containers for every run, then removed
                          ▼
                 ┌───────────────── shared network namespace, no external network ─┐
                 │ target container (untrusted)       runner container (trusted)   │
                 │ FastAPI app + hooks        ◀─127.0.0.1─  k6 + FaultForge core   │
                 │ no artifacts mount, no secrets           writes /out only       │
                 └─────────────────────────────────────────────────────────────────┘
```

- The target runs with `--network none`. The runner joins it with `--network container:<target>`, so it reaches the target on `127.0.0.1` and has no route anywhere else. The loopback rules in §4.9 stay unchanged.
- The target can't touch the artifacts directory, so it can't edit its own report. It can still lie through `/__ff/state`, which is disclosed; client counts are measured by the runner.
- The control plane validates `report.json` against the pydantic models before showing it, and treats its strings as untrusted.

## V3.3 Sandbox policy

Applied to both containers:

| Control | Setting |
|---|---|
| Network | No external network |
| Filesystem | `--read-only`, with a small `--tmpfs` for working data |
| Privileges | Non-root user, `--cap-drop ALL`, `--security-opt no-new-privileges` |
| Resources | `--memory`, `--cpus`, `--pids-limit` |
| Secrets | Allowlisted environment only; no keys and no Docker socket |
| Code freshness | `PYTHONDONTWRITEBYTECODE=1` and a fresh code directory with no `__pycache__`, so a retest can't run stale bytecode (`docs/VERIFIED.md`) |
| Lifetime | Wall-clock kill at the §4.6 budget; always removed afterwards |
| Leftovers | Containers are labelled; a reaper removes strays at startup and on a timer |

**Should have:** gVisor (`--runtime=runsc`) if it installs cleanly on the VM. Without it, containers share the VM's kernel, and the README says so.

## V3.4 The agent

Deliberately narrow, so that it is reliable:

1. Run the scenario on the buggy target and get a verified `FAIL`.
2. Send the report and `app.py` to Vultr Serverless Inference, marked as untrusted data, with the business rules. The model has no tools.
3. Accept one complete replacement for `app.py`, within a size limit. Other files can't change.
4. Run it in a **new sandbox** with `compare_to` set to the first run.
5. If the verdict isn't `FIXED`, send the new report back. Stop after **3 attempts** and report failure honestly.

FaultForge's verdict decides success, never the model. This gives a real retry moment: a fix that forgets to void payments fails the `orphan` invariant and the agent has to correct it.

## V3.5 Public URL safeguards

- **No uploads and no free-text prompts.** Users choose from fixed targets. Bring-your-own-code is FUTURE.
- One run at a time, with a queue limit and per-IP cooldown.
- Limits on tokens per attempt and tokens per day. The page keeps working for replays when the limit is hit.
- A stored run from a real execution is shown as a labelled replay when the system is busy. It is never presented as live.

## V3.6 Containment moment

A second fixed target, `hostile-build`, is real code that tries to:
- call an external server
- read environment variables looking for keys
- write outside its temporary directory
- exhaust memory and processes, and spin forever

The sandbox records each attempt and result: network unreachable, no secrets found, read-only filesystem, limit hit, killed at the deadline. The report is `INCONCLUSIVE`, never `PASS`, and the page shows the VM still healthy with no containers left behind.

## V3.7 Milestones

The internal target is **submission-ready by the end of 7 November UTC**, which leaves 8 November as a buffer until the organisers confirm the deadline. Stop for review after each milestone.

| # | Milestone | Acceptance | Vultr credits |
|---|---|---|---|
| M1 | `ticket-api`: buggy code, reference fix, sloppy and false fixes | §7 M1 checks. Failure-path tests limited to losing the claim, rollback and decline. | No |
| M2 | Hardened k6 runner, starting with the k6 2.3.0 option check | §7 M2 checks | No |
| M3 | Core loop and CLI: config, safety, budgets, oracle, report, compare | §7 M3a and M3b checks, excluding reload-based stale checks | No |
| M4 | Sandbox manager and hostile target | Buggy `FAIL`, fixed `PASS` and `FIXED` in sandboxes. Hostile target contained. Egress test fails to connect. No containers left after a timeout. Inference key absent from sandbox environment. | No |
| M5 | Web app, deployed to the VM | Public URL runs a real scenario end to end and shows the verdict and evidence. | **VM** |
| M6 | Agent on Vultr inference | From the public URL: `FAIL`, generated fix, `FIXED` with a new `build_id`. A bad fix is rejected and retried. Token limits enforced. | **Inference** |
| M7 | Submission | Timed rehearsal, video with containment moment, slides, cover image, README covering Vultr services and architecture, lablab entry. | Small |

**Deploy early:** M5 ships a working vertical slice before the agent exists, so a late problem can't leave us with nothing deployable.

**Cuts for this timeline:**
- Cut: the MCP server (a thin wrapper later if time remains), scenario generation (M7 of v2.1), reload-based fingerprint flows, `doctor`, the Makefile, and full evidence paging.
- First to cut if behind: gVisor, hypotheses beyond the demo's rules, then the serial control.
- Never cut: sandbox policy, budgets, verdict rules, real execution, and the containment moment (it's required for submission).

### M3 decisions

- Transport and unexpected-status violations only count from completed trials, because a restart or kill also breaks connections.
- `capacity` checks run on incomplete trials, so they must be client-observed upper bounds: `client.<counter> <= <expr>` or `<`, where the right side uses only `before.*` and integers. Other forms are `config_invalid`.
- `Comparison.fix_verified` is what the agent loop in M6 must use, not per-invariant `FIXED`. An invariant can be `FIXED` in a retest that still has unexpected 500s. `fix_verified` is true only when the candidate's verdict is `PASS`, at least one invariant is `FIXED`, and none is `NOT_VERIFIED`, `STILL_FAILING` or `REGRESSED`.
- M4 must bound k6's memory, because k6 buffers response bodies when `capture_field` is set, and must prove that a memory kill yields a report or a structured failure.

## V3.8 Demo video (about 2 minutes)

| Time | Scene |
|---|---|
| 0:00 | Unit tests pass and the API looks healthy |
| 0:15 | Run load from the public URL: `FAIL`, many tickets sold for 1 in stock, all 200s and low latency |
| 0:45 | Agent reads the evidence and proposes a fix through Vultr inference |
| 1:05 | New sandbox, same workload: `PASS`, `FIXED`, new `build_id` |
| 1:25 | **Containment:** run `hostile-build`, and each attack is blocked and shown |
| 1:50 | Close on the architecture and the Vultr services used |

## V3.9 Costs

Nothing is created without the owner's approval, with the current price shown first. One VM with roughly 2–4 vCPU and 4–8 GB should be well within the $200. Vultr's docs were reported at $0.55 / $2.75 per million input / output tokens for inference, which is unverified against the console. Confirm both prices in the console before use.

---

# FaultForge — Architecture & Hackathon MVP Plan (v2.1)

Status: **superseded in part by v3 above.** The track decision in §11 is resolved: Blast Radius Zero.
Audience: the team, and the Claude Code session that will implement it.

---

## What changed in v2.1

The project is confirmed greenfield, so the reconnaissance gate is removed.

| # | Gap in v2 | v2.1 decision | Where |
|---|---|---|---|
| 1 | A fix that rejects every buyer passed | New `liveness` invariant: `client.accepted >= before.stock` | §4.1, §4.4 |
| 2 | Removing the payment call passed | New `integrity` invariant: `after.orders_without_payment == 0` | §4.1, §4.4, M1 |
| 3 | PASS needed no real concurrency | Concurrent trials must reach `min_peak_in_flight`, or they are incomplete | §4.7 |
| 4 | `config_hash` contents were undefined | Defined; it covers the effective load | §4.10 |
| 5 | A retest could use a different plan | `compare_to` inherits the baseline's plan | §3.2, §4.10 |
| 6 | Clamping contradicted "trials are never reduced" | Over-limit `trials` is rejected; only per-trial load values clamp | §4.6 |
| 7 | `PASS_UNVERIFIED_BUILD` contradicted §4.10 | Removed; use `NOT_VERIFIED` with a reason | §4.8, §4.10 |
| 8 | No comparison rule for `not_evaluable` | Either side `not_evaluable` gives `NOT_VERIFIED` | §4.10 |
| 9 | No incomplete reason for a failed reset | Added `reset_failed` | §4.7 |
| 10 | Multi-worker checks would falsely report restarts | Worker test stays in a helper script; SQLite busy timeout required | M1 |
| 11 | "Attached" was undefined, so buggy code could look orphaned | Defined: a hold is attached if an order references it | M1 |
| 12 | Schema drift check relied on CI that doesn't exist | Now a `pytest` test | M0 |
| 13 | k6 1.x was already out of date | Pinned to k6 2.3.0 | §2, M0 |

---

## What changed from v1

| # | Request | v1 | v2 decision | Where |
|---|---|---|---|---|
| 1 | Reconcile with existing work | Assumed greenfield | **Reconnaissance gate (M0)** with a reuse-or-adapt rule. No existing code was visible during planning (see 1.1). | §1, M0 |
| 2 | 409 counted as error | k6 default marks every status ≥400 as failed | k6 `setResponseCallback` declares accepted + rejected statuses as expected; FaultForge computes its own outcome-based error rate | §4.2, §4.4 |
| 3 | Multi-trial budgets, incomplete runs | Per-trial caps only; PASS/FAIL/ERROR | **Aggregate request and wall-clock budgets per tool call**, checked at admission and enforced by deadline. New **INCONCLUSIVE** verdict. Never PASS on an incomplete run. | §4.6, §4.7 |
| 4 | Build fingerprint | Hash of `app.py` on disk | **Hash of loaded code objects, captured at server start**, plus a per-process `instance_id` and a stale-source check | §4.8 |
| 5 | Atomic fix correctness | Conditional UPDATE only | **Authorize → atomic claim + order in one transaction → void on any failure.** New invariant: no authorization hold without a ticket. Tests for declines, timeouts and rollback. | §7 (M1), §4.1 |
| 6 | Local execution hardening | Loopback check only | No redirects; canonical loopback origin only; validated paths with origin re-check; k6 IP blocklist, sanitised environment, no usage report; filesystem path containment | §4.9 |
| 7 | Metrics vs evidence | Mixed in report | `performance` = aggregates only. Per-request evidence lives in `requests.jsonl`, referenced by hash; the report carries bounded samples only. | §4.3, §4.5 |
| 8 | Proven vs suspected | One `findings.class` mixing symptom and cause | **`violations`** (proven facts) separate from **`hypotheses`** (suspected causes, rule-generated, labelled). Optional serial control trial upgrades a hypothesis to "supported". | §4.4 |
| 9 | Scenario generation | NICE | **SHOULD**, gated behind a reliable core demo. Adds `faultforge_validate_scenario` and an authoring guide. | §3.4, M7 |

**Scope removed to pay for this (see §5):** performance SLO checks (aggregates still reported), the nonlocal-target allowlist, IPv6 targets, the k6 web dashboard, the second scenario, the ramp profile, the HTML comparison view, and the hand-written JSON Schema (now generated from pydantic models).

---

## 0. Instructions for the implementing Claude Code session

1. **The project is greenfield (§1).** Start with M0; don't search for existing code.
2. Build **one milestone at a time** (§7). Run its acceptance checks, then stop for human review.
3. Don't add scope. Anything outside MUST/SHOULD needs explicit approval. M7 (scenario generation) starts only after M6 passes.
4. Never weaken a guardrail (§4.9) or a budget (§4.6) to make something pass. If one blocks you, stop and ask.
5. The MCP server uses **stdio**: only protocol messages on stdout. Logs go to stderr; k6 output is captured, never inherited.
6. Verdicts and violations are deterministic code. Hypotheses are rule-generated and always labelled as suspected.
7. Items marked **(verify)** depend on tool versions. Check them against the pinned k6, the MCP Python SDK and current Claude Code docs before relying on them, and record the result in `docs/VERIFIED.md`.

---

## 1. Starting point and reconciliation

### 1.1 Confirmed during planning
- The planning workspace had no FaultForge code.
- The claude.ai project had no docs besides this plan.
- There were no earlier conversations about a reporting pipeline.
- A GitHub repository listing was not run, at the user's request.

**Confirmed by the project owner (2026-11-06):** there is no existing FaultForge code, including reporting. Everything is built new, and no reconnaissance step is needed.

---

## 2. Architecture (unchanged shape)

```
 Developer ──▶ Claude Code ──(MCP stdio)──▶ FaultForge MCP server (thin)
                   ▲                               │
                   │                               ▼
                   │                   FaultForge core (library; CLI uses it too)
                   │                    ├─ config     faultforge.yaml → pydantic
                   │                    ├─ safety     origin/path/fs validation, caps
                   │                    ├─ budget     aggregate requests + wall clock
                   │                    ├─ lifecycle  admission → trials → settle → oracle
                   │                    ├─ runner/k6  fixed scripts, hardened argv/env, kill
                   │                    ├─ oracle     violations (proven) + hypotheses (suspected)
                   │                    ├─ report     RunResult → report.json / report.md / compare
                   │                    └─ store      .faultforge/runs/<run_id>/
                   │                               │  HTTP, 127.0.0.1 only, no redirects
                   │                               ▼
                   │                   ticket-api (FastAPI + SQLite + in-process payment stub)
                   │                    ├─ POST /reserve
                   │                    └─ test hooks: /health, POST /__ff/reset, GET /__ff/state
                   └──── compact summary (Proven / Suspected / Performance) + run_id
```

**Stack:** Python 3.11+, `uv`, MCP Python SDK (verify the API), pydantic v2, PyYAML, httpx, and pinned k6 2.3.0. The demo target uses FastAPI, uvicorn and stdlib `sqlite3` (WAL mode). Pydantic models are the **source of truth for the report contract**; `schemas/report.schema.json` is generated from them.

**Performance vs correctness:** unchanged from v1, and now enforced structurally.
- Correctness lives in `violations`.
- Performance lives in `performance`, which holds aggregates only.
- No performance number affects the verdict in the MVP, because SLO checks were cut.

---

## 3. MCP tools

### 3.1 `faultforge_list_scenarios`
**Input:** `{ project_dir? }`

**Output:**
- target origin, reachability, `build` (§4.8) and a stale-source flag
- for each scenario:
  - id and description
  - `mutates_data`
  - default load
  - invariant names
  - `validated` and `user_confirmed` flags
- the hard limits and budgets (§4.6)

### 3.2 `faultforge_run_scenario` (synchronous)

**Input:**
```json
{
  "scenario_id": "last-ticket-rush",
  "project_dir": "optional",
  "load": { "vus": 20, "iterations": 20, "max_duration_s": 10 },
  "trials": 3,
  "serial_control": true,
  "compare_to": "run_…"
}
```

**Retests:** `compare_to` inherits the baseline's effective load, `trials` and `serial_control`. Passing a different value for any of them returns `NOT_COMPARABLE` without sending load.

**Admission:** the full plan is checked against the aggregate budgets before anything runs. An over-budget plan is **rejected** with `budget_exceeded` and a suggested smaller plan. Trials are never silently reduced, because that would change what "verified" means.

**Output:** a compact report of 60 lines or fewer. It has a **Proven** section, a **Suspected** section, a completeness line, a performance line, an optional comparison, a `run_id` and file paths.

**Errors:** structured, never raised.
```
{status: "error",
 error_class: unsafe_target | target_unreachable | k6_missing | config_invalid
            | budget_exceeded | busy | engine_failed,
 message, fix_hint}
```

### 3.3 `faultforge_get_run`
**Input:** `{ run_id, section: "summary" | "violations" | "hypotheses" | "performance" | "evidence", offset?, limit? (≤ 100) }`

`section: "evidence"` pages through `requests.jsonl` with optional filters (`trial`, `outcome`).

### 3.4 `faultforge_validate_scenario` (SHOULD, M7)
**Input:** `{ project_dir?, scenario_id? }`. It reads `faultforge.yaml` from disk; Claude writes that file through normal, user-approved edits.

**Checks:**
- schema
- safety (§4.9)
- the expression whitelist
- every invariant variable exists in one **read-only** `GET /__ff/state`
- a `reset` hook exists if `mutates_data` is set

**Output:** `{ valid, errors[], warnings[], user_confirmed }`. It never calls reset and never sends load.

**Still not exposed:** cancel, start/poll, arbitrary scripts, arbitrary URLs.

---

## 4. Contracts and execution

### 4.1 `faultforge.yaml` (demo)

```yaml
version: 1
target:
  origin: http://127.0.0.1:8000        # canonical loopback only (§4.9)
  health: /health
  reset: { method: POST, path: /__ff/reset, json: { stock: 1 } }
  state: /__ff/state

scenarios:
  - id: last-ticket-rush
    description: 20 buyers race for the last ticket
    mutates_data: true
    user_confirmed: true                # invariants are business rules a human signed off
    script: burst_request
    request:
      method: POST
      path: /reserve
      json: { item_id: 1, buyer: "buyer-${VU}-${ITER}" }   # substitution in JSON values only
    outcomes:
      accepted_status: [200, 201]
      rejected_status: [409]            # expected business rejection, NOT an error
      capture_field: order_id
    load: { vus: 20, iterations: 20, max_duration_s: 10, min_peak_in_flight: 10 }
    invariants:
      - name: No more tickets confirmed than were in stock
        type: capacity                  # client-observed upper bound; evaluable on incomplete trials
        check: client.accepted <= before.stock
      - name: Stock accounting balances
        type: accounting
        check: before.stock - after.stock == after.orders_confirmed - before.orders_confirmed
      - name: Every confirmation is recorded
        type: consistency
        check: client.accepted == after.orders_confirmed - before.orders_confirmed
      - name: No customer holds a payment authorization without a ticket
        type: orphan
        check: after.payments_unattached == 0
      - name: Every order has a payment authorization
        type: integrity
        check: after.orders_without_payment == 0
      - name: Available stock can actually be sold
        type: liveness                  # catches a "fix" that rejects everyone
        check: client.accepted >= before.stock
      - name: Stock never negative
        type: bound
        check: after.stock >= 0
```

The `liveness` check above assumes more buyers than stock and no declined payments, which holds for this scenario.

Invariant `type` is an enum: `capacity | accounting | consistency | orphan | integrity | liveness | bound | custom`. It determines the violation type and whether the invariant can be evaluated on an incomplete trial (§4.7).

### 4.2 k6 script library (fixed, parameterised)

`scripts/burst_request.js` uses a `shared-iterations` executor. Parameters arrive as one JSON env var (`-e FF_SPEC=…`).

**Expected statuses (fix for #2):**
- `http.setResponseCallback(http.expectedStatuses(...accepted, ...rejected))` (verify), built from config.
- As a result, 409s don't count toward k6's `http_req_failed`.
- FaultForge does **not** use `http_req_failed` for verdicts or error rates. It uses its own outcome counts (§4.4). k6's figure is kept only as a cross-check.

**Per-request classification**, done in-script:

| Outcome | Condition |
|---|---|
| `accepted` | status in `accepted_status` |
| `rejected` | status in `rejected_status` |
| `unexpected` | any other non-zero status, including any 3xx |
| `transport_error` | status 0 only; k6 also sets `error_code` on HTTP 4xx and 5xx (see `docs/VERIFIED.md`) |

**Evidence metric:** one custom Trend, `ff_req`. Its value is the request duration in ms, with tags `{outcome, status, request_id, vu, iter, trial, captured}`.
- High-cardinality tags appear **only** on this metric.
- Built-in HTTP metrics get a fixed `name` tag, and system tags are restricted (`--system-tags=status,method,name,expected_response,error_code`, verify) to keep cardinality bounded.

**Request ids:** every request carries `X-FF-Request-Id`.

**Other script settings:**
- `handleSummary` writes `k6-summary.json`, the k6-native aggregates.
- k6 thresholds are not used. Exit code 0 means k6 completed; it does not mean the trial completed (§4.7).

### 4.3 Artifacts and the metrics/evidence split (#7)

```
.faultforge/runs/<run_id>/
  report.json          aggregates + violations + hypotheses + bounded samples (≤ 50 KB target)
  report.md            human rendering of report.json
  requests.jsonl       ALL per-request evidence, one line per request, all trials
  trials/<n>/k6-summary.json, state-before.json, state-after.json, k6.log (tail-capped 200 KB)
```

- Raw `--out json` output is streamed to a temp file and **filtered to `ff_req` samples** into `requests.jsonl`. The raw file is then deleted.
- Each `requests.jsonl` line is `{trial, request_id, vu, iter, t_start, t_end, latency_ms, status, outcome, captured}`. `t_start` and `t_end` are `Date.now()` taken around each request.
- `report.json` references the file as `{path, count, sha256}`. It never inlines more than 10 samples per violation.
- **Aggregates are computed by FaultForge** from `requests.jsonl`: percentiles across trials and by outcome. Percentiles can't be averaged across trials, so they are computed this way rather than taken from k6. Throughput and iteration counts come from each trial's `k6-summary.json`.
- For a single trial, FaultForge's p95 must match k6's p95 within 1 ms (M3 cross-check).

### 4.4 Oracle: proven violations vs suspected hypotheses (#8)

**Client counters per trial:** `accepted`, `rejected`, `unexpected`, `transport_error`, `total`.

**Rates:**
- `error_rate = (unexpected + transport_error) / total`
- `rejection_rate = rejected / total` (reported, never an error)

**Expressions** are parsed with Python `ast` against a whitelist of nodes:
- allowed: ints, dotted names in `before.*`, `after.*` or `client.*`; `+ - * //`; comparisons; `and`, `or`, `not`
- not allowed: calls, subscripts, attribute access outside that namespace, `eval`
- an unknown variable produces `config_invalid`

**Violations (proven, deterministic).** A violation records:
- the observed values
- the trials it occurred in (e.g. `3/3`)
- references to evidence samples

| Violation type | Raised when |
|---|---|
| `invariant.capacity_exceeded` | a `capacity` invariant fails |
| `invariant.accounting_mismatch` | an `accounting` invariant fails |
| `invariant.inconsistent_state` | a `consistency` invariant fails |
| `invariant.orphaned_resource` | an `orphan` invariant fails |
| `invariant.integrity_violated` | an `integrity` invariant fails |
| `invariant.liveness_failed` | a `liveness` invariant fails |
| `invariant.bound_violated` | a `bound` invariant fails |
| `invariant.custom_failed` | a `custom` invariant fails |
| `response.unexpected_status` | `unexpected > 0` (with a status breakdown) |
| `transport.errors` | `transport_error > 0` |

Facts attached to violations as evidence (not causes):
- `accepted_overlap`: whether all accepted requests' `[t_start, t_end]` intervals share a common instant, and the width of the window
- the per-status distribution

**Hypotheses (suspected, rule-based).** Each hypothesis has:
- `status: suspected | supported`
- `basis[]`: the facts it rests on
- `explains[]`: violation ids
- `how_to_confirm`: what to check in code

| Rule | Hypothesis |
|---|---|
| capacity_exceeded and accepted_overlap | `check_then_act_race` (TOCTOU between availability check and write) |
| accounting_mismatch with stock decrement < confirmed orders | `lost_update` (non-atomic read-modify-write) |
| orphaned_resource | `missing_compensation` (a failure path doesn't release or void) |
| integrity_violated | `required_step_bypassed` (an order exists without its required payment step) |
| liveness_failed | `over_rejection` (requests that should succeed are being refused) |
| unexpected 5xx only under concurrency | `concurrency_fault` (e.g. lock contention, unhandled conflict) |

**Upgrading a hypothesis:**
- A hypothesis becomes `supported` only when the **serial control trial** (same requests, `vus: 1`) shows the related invariants holding. That is evidence the failure depends on concurrency.
- A hypothesis is never "proven". The report states this: *FaultForge observes black-box behaviour; root cause must be confirmed in code.*

**Rendering:** `report.md` and the MCP output always render **Proven** and **Suspected** as separate headed sections.

### 4.5 `report.json` (v2)

```json
{
  "schema_version": "2.1",
  "run_id": "run_20261106_071900_ab12",
  "scenario_id": "last-ticket-rush",
  "verdict": "FAIL",
  "verdict_reason": "2 proven invariant violations",
  "completeness": {
    "status": "complete",
    "trials_planned": 3, "trials_completed": 3, "trials_incomplete": 0, "trials_skipped": 0,
    "serial_control": "completed"
  },
  "budget": {
    "max_requests_total": 2000, "requests_planned": 80, "requests_sent": 80,
    "max_wall_s_total": 60, "wall_s_used": 7.9, "clamped": []
  },
  "target": {
    "origin": "http://127.0.0.1:8000",
    "build": { "build_id": "c0de1a2b", "instance_id": "7f3e…", "instance_started_at": "…", "stale_source": false },
    "build_stable_during_run": true
  },
  "config": { "load": { "vus": 20, "iterations": 20, "max_duration_s": 10, "min_peak_in_flight": 10 },
              "trials": 3, "serial_control": true, "config_hash": "sha256:…",
              "engine": { "name": "k6", "version": "1.x.y" }, "mutates_data": true, "user_confirmed": true },
  "trials": [
    { "trial": 1, "kind": "concurrent", "status": "completed", "incomplete_reason": null,
      "planned_requests": 20, "completed_requests": 20, "peak_in_flight": 20,
      "client": { "accepted": 14, "rejected": 6, "unexpected": 0, "transport_error": 0 },
      "state_before": { "stock": 1, "orders_confirmed": 0, "payments_unattached": 0, "orders_without_payment": 0 },
      "state_after":  { "stock": 0, "orders_confirmed": 14, "payments_unattached": 0, "orders_without_payment": 0 },
      "invariants": [ { "name": "No more tickets confirmed than were in stock", "result": "fail",
                        "values": { "client.accepted": 14, "before.stock": 1 } } ] }
  ],
  "violations": [
    { "id": "V1", "type": "invariant.capacity_exceeded", "proven": true,
      "invariant": "No more tickets confirmed than were in stock", "trials_violated": "3/3",
      "observed": { "max_client.accepted": 15, "before.stock": 1 },
      "facts": { "accepted_overlap": true, "overlap_window_ms": 47 },
      "evidence": { "file": "requests.jsonl", "filter": { "outcome": "accepted" }, "sample": [ "…≤10 lines…" ] } },
    { "id": "V2", "type": "invariant.accounting_mismatch", "proven": true, "trials_violated": "3/3" }
  ],
  "hypotheses": [
    { "id": "H1", "suspected_cause": "check_then_act_race", "status": "supported",
      "explains": ["V1"], "basis": ["capacity_exceeded", "accepted_overlap", "serial control: invariants held"],
      "how_to_confirm": "Look for a read of availability followed by an await/IO before the write." },
    { "id": "H2", "suspected_cause": "lost_update", "status": "supported", "explains": ["V2"] }
  ],
  "performance": {
    "scope": "completed requests of concurrent trials",
    "latency_ms": { "p50": 58, "p95": 72, "p99": 75, "max": 81 },
    "latency_ms_by_outcome": { "accepted": { "p50": 60, "p95": 73 }, "rejected": { "p50": 3, "p95": 5 } },
    "throughput_rps_per_trial": [6.1, 6.4, 6.2],
    "error_rate": 0.0, "rejection_rate": 0.27,
    "status_counts": { "200": 44, "409": 16 }
  },
  "evidence": { "requests": { "path": "requests.jsonl", "count": 80, "sha256": "…" } },
  "comparison": null,
  "reproduce": { "cli": "faultforge run last-ticket-rush --trials 3 --serial-control",
                 "k6": "k6 run --no-usage-report … scripts/burst_request.js" },
  "disclosures": [
    "Scenario mutates data; state reset before each trial.",
    "Payment provider is an in-process stub with a fixed 50 ms delay."
  ]
}
```

### 4.6 Budgets (#3)

Hard caps are code constants. Per-trial load values (`vus`, `iterations`, `max_duration_s`) are clamped to them, and every clamp is listed in `budget.clamped`. The clamped values are the effective load that goes into `config_hash`. `trials` is never clamped: more than `MAX_TRIALS` is rejected with `budget_exceeded`.

| Constant | Value | Scope |
|---|---|---|
| `MAX_VUS` | 50 | per trial |
| `MAX_ITERATIONS_PER_TRIAL` | 500 | per trial |
| `MAX_TRIAL_DURATION_S` | 20 | per trial |
| `MAX_TRIALS` | 5 | per call (the serial control counts as a trial) |
| `MAX_REQUESTS_TOTAL` | 2000 | **per tool call, across all trials and the control** |
| `MAX_WALL_S_TOTAL` | 60 | **per tool call**, from admission to report written |
| `TRIAL_OVERHEAD_S` | 3 | reserved per trial for reset, snapshots, k6 startup and settle |

**Admission.** The plan is rejected with `budget_exceeded` (and a suggested plan) if either:
- `Σ planned_requests > MAX_REQUESTS_TOTAL`, or
- `Σ (max_duration_s + TRIAL_OVERHEAD_S) > MAX_WALL_S_TOTAL − 5`

**Enforcement.** A run deadline is set at admission.
- Each trial's k6 `maxDuration` and kill timeout are the smaller of its configured duration and `deadline − now − TRIAL_OVERHEAD_S`.
- If too little time remains for a trial, that trial and all later trials are **skipped**.
- Requests are counted from `requests.jsonl` after each trial. If the actual count would exceed the remaining request budget, later trials are skipped.

**Timeouts:**
- Hook calls (health, reset, state) use a 2 s timeout each and count toward the wall budget.
- The Claude Code MCP tool timeout must exceed `MAX_WALL_S_TOTAL` plus margin. Set `MCP_TOOL_TIMEOUT` in the demo environment (verify the variable and its default).

### 4.7 Completeness and verdicts (#3)

A trial is **completed** only if **all** of the following hold:
- k6 exited 0 and was not killed
- `iterations` equals the plan, with no `dropped_iterations`
- the `requests.jsonl` count for the trial equals the plan
- before and after state snapshots succeeded
- the same `instance_id` was seen before and after the trial
- for concurrent trials, `peak_in_flight` reached `min_peak_in_flight`

`peak_in_flight` is the highest number of overlapping `[t_start, t_end]` intervals across **all** requests in the trial. `min_peak_in_flight` defaults to `max(2, vus // 2)`. The serial control is exempt.

Otherwise the trial is **incomplete**, with a reason:

| Reason | Meaning |
|---|---|
| `timeout` | k6 was killed at its deadline |
| `iterations_short` | fewer iterations ran than planned |
| `engine_exit_nonzero` | k6 exited with a nonzero code |
| `target_restarted` | `instance_id` changed during the trial |
| `reset_failed` | the reset hook failed, or stale data survived it |
| `state_snapshot_failed` | a before or after snapshot failed |
| `insufficient_contention` | `peak_in_flight` stayed below `min_peak_in_flight`, so the race wasn't exercised |
| `request_count_mismatch` | `requests.jsonl` count differs from the plan |

A trial that never started is **skipped** (`budget_exhausted`).

**Settle step.** After k6 ends, FaultForge polls `/__ff/state` until `in_flight == 0`, if the target exposes it, with a 2 s cap before taking the after-snapshot. If the target never settles, the trial is incomplete with reason `state_snapshot_failed`.

**Evaluating invariants on incomplete trials.**
- `capacity` invariants are client-observed upper bounds, so a violation is still proof.
- All other types are marked `not_evaluable`, because state may be mid-transition.

**Verdicts:**

| Verdict | Rule |
|---|---|
| `ERROR` | No trial started (preflight, config, safety or busy failure) |
| `FAIL` | At least one proven violation in any evaluable result, even if other trials are incomplete |
| `PASS` | All planned trials **completed**, every invariant evaluated and passed in every trial, no unexpected or transport outcomes |
| `INCONCLUSIVE` | No proven violation, but at least one trial incomplete or skipped, or at least one invariant `not_evaluable` |

The MCP output always shows completeness next to the verdict, e.g. `PASS (3/3 trials complete)` or `INCONCLUSIVE (1/3 complete: timeout)`.

### 4.8 Build fingerprint of loaded code (#4)

The target computes this **once at process start**, after its modules are imported. With uvicorn `--reload` (verify), every reload is a fresh worker process.

| Field | How it's computed |
|---|---|
| `build_id` | sha256 over the **loaded function code objects** of the app's own modules, taken from `sys.modules` (not from disk). Each function's `__code__` is serialised with `marshal.dumps`, including functions in nested classes. The input is ordered by qualified name and includes the Python version. This reflects what is executing, not what is on disk. |
| `instance_id` | a random UUID per process; it changes on every restart, even if the code is identical |
| `instance_started_at` | the process start time |
| `stale_source` | computed at each `/__ff/state` call: true if any app source file's mtime is later than `instance_started_at`, i.e. the code on disk was edited but the running process hasn't reloaded it |

Limitations, documented:
- Line-number changes from comment-only edits also change `build_id`.
- Dynamically generated code isn't covered.
- The fingerprint is self-reported by a test hook, which is acceptable for local development targets.
- For apps without `build`, comparison reports `build: unknown` and can't return `FIXED`; it returns `NOT_VERIFIED` with reason `build_unknown`.
- Module-level constants aren't function code, so a change only to one of them leaves `build_id` unchanged and gives `NOT_VERIFIED` with reason `build_unchanged`.

### 4.9 Safety boundaries (#6)

**Target origin:**
- Scheme must be `http`. No userinfo. Explicit port.
- The host must be the literal `127.0.0.1`. `localhost` is accepted and normalised to `127.0.0.1` after resolution confirms loopback.
- Parsing uses `ipaddress` and accepts **only the canonical dotted form**, so `0x7f.1`, `2130706433` and `127.1` are rejected.
- IPv6 and nonlocal targets are refused in the MVP.

**Paths** must:
- start with a single `/`
- contain no scheme, `//`, `@`, `\`, `#`, `..` segment, percent-encoded `/` `\` `.`, or control characters
- be at most 256 characters long

Each URL is built from the validated origin plus the path, then **re-parsed**, and its scheme, host and port must equal the origin's. Templating (`${VU}`, `${ITER}`) is allowed only inside JSON string values, never in paths or headers. Request bodies are capped at 16 KB.

**Redirects:**
- k6 runs with `maxRedirects: 0`, so any 3xx is an `unexpected` outcome.
- FaultForge's own hook client uses httpx with `follow_redirects=False`; a 3xx from a hook is a hard error.

**Stopping unintended external requests (defence in depth):**
- k6 is started with:
  - `--no-usage-report`
  - the REST API address disabled or bound to an ephemeral loopback port (verify `--address`)
  - **an IP blocklist covering every IPv4 range except 127.0.0.0/8**: `0.0.0.0/2, 64.0.0.0/3, 96.0.0.0/4, 112.0.0.0/5, 120.0.0.0/6, 124.0.0.0/7, 126.0.0.0/8, 128.0.0.0/1` (verify the option name, `blacklistIPs`)
- k6 runs with a **minimal environment**: `PATH`, `HOME` (a scratch dir) and `TMPDIR`. All other `K6_*` variables are excluded, so ambient settings such as `K6_OUT` or `K6_CLOUD_TOKEN` can't add cloud outputs. The argv is built as a list, never with `shell=True`.
- The demo target's payment provider is in-process, so it makes no outbound calls.

**Filesystem:**
- `project_dir` must resolve, via `realpath`, inside an allowed root: the server's start directory or `FAULTFORGE_ROOTS`.
- `run_id` must match `^run_\d{8}_\d{6}_[a-f0-9]{4}$`.
- Every artifact path is resolved and checked to be inside the runs directory, which blocks symlink escapes.
- The script name must be a key in the built-in library.

**Process and run safety (unchanged from v1):**
- k6 runs in its own process group; it gets SIGTERM, then SIGKILL after 3 s.
- Cleanup runs on deadline, server exit and signals.
- Only one run at a time (file lock).
- `mutates_data` is disclosed in the output, and mutating scenarios require a reset hook.
- Claude Code's tool permission prompt is the human approval step.

### 4.10 Comparison rules (`compare_to`)

Comparison is **only valid** when the baseline and candidate share `scenario_id`, `config_hash`, `trials` and `serial_control`. Otherwise the result is `NOT_COMPARABLE`.

`config_hash` is the sha256 of canonical JSON covering the effective load, request template, outcome statuses, invariants, target hook paths, k6 script hash and k6 version. So a `vus: 1` rerun can't be compared with a concurrent baseline.

| Invariant result | Conditions |
|---|---|
| `FIXED` | baseline FAIL on it; candidate **PASS with complete trials**; `build_id` changed; candidate not `stale_source` |
| `NOT_VERIFIED` | candidate passes, but trials were incomplete, the build was unchanged or unknown, or the source was stale; **or** the invariant was `not_evaluable` on either side. Always carries a reason: `trials_incomplete`, `build_unchanged`, `build_unknown`, `stale_source`, `not_evaluable`. |
| `REGRESSED` | baseline passed and candidate failed |
| `STILL_FAILING` | both failed |
| `STILL_PASSING` | both passed |

Performance deltas (p50, p95, throughput) are shown for information only.

---

## 5. MVP scope (revised)

**MUST HAVE**
- M0 pydantic contracts
- `ticket-api` with:
  - the genuine race
  - the payment stub (authorize / void / decline / timeout)
  - the loaded-code fingerprint, `instance_id`, `stale_source` and `in_flight`
  - passing sequential unit tests
  - a reference fix with failure-path tests
- `faultforge.yaml` loader with typed invariants and the expression whitelist
- Hardened k6 runner: expected statuses, no redirects, IP blocklist, sanitised env, kill, completeness detection
- Safety: origin, path and filesystem validation, caps, lock
- Aggregate budgets, the INCONCLUSIVE verdict and per-trial completeness
- Oracle producing violations; separation of evidence (`requests.jsonl`) from aggregates
- `report.json`, `report.md` and CLI (`list`, `run`, `show`, `compare`)
- MCP server: `list_scenarios`, `run_scenario`, `get_run`
- `compare_to` with the §4.10 rules

**SHOULD HAVE**
- Hypotheses with rule-based `suspected` status *(cheap once violations exist)*
- `trials` greater than 1 (default 3 for verification)
- Serial control trial (upgrades hypotheses to `supported`)
- `faultforge doctor`; demo `Makefile`
- **Scenario generation (M7):** `faultforge_validate_scenario`, an authoring guide exposed as an MCP prompt/resource, and `user_confirmed` gating. Starts only after M6.

**NICE TO HAVE**
- Performance SLO checks (demoted from v1 SHOULD; aggregates are still reported)
- An MCP progress notification per trial

**FUTURE**
- Nonlocal targets and allowlist; IPv6
- Cloud or distributed execution; async run API with cancel
- k6 web dashboard; observability and trace correlation
- More scenarios (idempotency, double-charge); ramp and soak profiles
- HTML diff view; OpenAPI-based discovery; CI integration

**Explicitly cut:**
- dynamic k6 script generation
- Docker orchestration
- a run database
- auth and multi-user support
- a hand-written JSON Schema

---

## 6. Feasibility

**Estimated effort for MUST HAVE:** about **20–24 focused hours**, up from about 14–18 in v1. The added cost is mostly:
- the payment and transaction work (M1)
- k6 hardening (M2)
- budgets and completeness (M3a)

**SHOULD HAVE, excluding M7:** about 4 hours. **M7:** about 3–4 hours.

**Threats, ranked:**

1. **k6 option details** (`setResponseCallback`, `blacklistIPs`, `maxRedirects`, `--address`, `--system-tags`, dropped-iteration reporting). If an option is missing or renamed in the pinned version, the hardening design changes.
   *Mitigation:* the first task of M2 is a 30-minute spike that verifies each option and writes `docs/VERIFIED.md`.
   *Fallback if the IP blocklist is unavailable:* rely on the fixed script, the validated origin and `maxRedirects: 0`, and note the gap.

2. **Larger live fix.** Claude's fix must now also void the payment hold, which takes more editing time on stage.
   *Mitigations:*
   - The failure path is pre-structured in the buggy code (§7, M1), which keeps the diff to about 10–15 lines.
   - The `orphan` invariant catches an incomplete fix, which is itself a good demo moment.
   - Rehearse.

3. **Total time versus the MCP tool timeout.** If the timeout can't be raised, lower `MAX_WALL_S_TOTAL` to fit, at the cost of fewer trials.

4. **Scope pressure.** If the team is behind at M5, the order of cuts is:
   1. M7
   2. the serial control
   3. hypotheses (the report then shows Proven only)
   4. trials greater than 1 (`FIXED` remains valid with 1 complete trial, but is weaker)

   Never cut: budgets, completeness, safety, or the 409 fix.

---

## 7. Milestones (critical path order)

### M0 — Contracts (S)
**Tasks:**
- `git init` and `uv` project setup
- pydantic models: `ScenarioConfig`, `TrialResult`, `RunResult`, `Report`
- `schemas/report.schema.json` generated from the models
- fixture reports: FAIL, PASS, INCONCLUSIVE
- pin k6 2.3.0, installed by a checksum-verifying script

**Acceptance:**
- `uv run pytest` passes: the fixtures validate against the models, and a test fails if the generated schema is out of date.
- `k6 version` prints the pinned version.

### M1 — `ticket-api` demo target (M) — *parallel with M2*

**Buggy handler** (the bug is check-then-act across an await, plus a lost update):
```python
stock = db.get_stock(item_id)                    # read
if stock <= 0: return 409
auth = await payments.authorize(buyer, key=req_id)   # Declined → 402 (nothing to undo yet)
db.set_stock(item_id, stock - 1)                 # write computed from a stale read
order_id = db.insert_order(item_id, buyer, auth.id)  # separate autocommit
return 200, {order_id}
```

**Reference fix** (kept in `demo/ticket-api/reference_fix/` for rehearsal and tests only):
```python
auth = await payments.authorize(buyer, key=req_id)   # Declined → 402, Timeout → void(key), 503
try:
    with db.tx_immediate():                      # BEGIN IMMEDIATE … COMMIT / ROLLBACK; no await inside
        if db.claim_stock(item_id) == 0:         # UPDATE … SET stock = stock - 1 WHERE id=? AND stock > 0
            claimed = False
        else:
            order_id = db.insert_order(item_id, buyer, auth.id)   # same transaction
            payments.attach(auth.id, order_id)                     # same DB, same transaction
            claimed = True
except Exception:
    await payments.void(auth.id)                 # rollback happened; release the hold
    raise                                        # → 500, counted as unexpected
if not claimed:
    await payments.void(auth.id)
    return 409
return 200, {order_id}
```

**Design rules:**
- Never hold a DB transaction across an `await`.
- Stock claim, order insert and payment attachment are one atomic transaction.
- Every authorization ends up either attached to an order or voided. A hold counts as attached when an order references it, so the buggy handler's accepted orders aren't reported as orphans.
- Set a SQLite busy timeout, so write contention waits instead of surfacing as a 500.
- `void` is idempotent; a failed void leaves status `void_pending`, which counts as unattached, so the oracle sees it.
- Authorize-then-claim produces short-lived holds for losing buyers. Reserve-then-authorize is the production alternative and is documented, not built.

**Supporting pieces:**
- **Payment stub:** a `payments` table in the same SQLite DB, with authorize (50 ms delay via `FF_AUTH_DELAY_MS`), void, attach, and deterministic decline and timeout switches for tests.
- **Test hooks:**
  - `/__ff/reset` (stock, orders and payments)
  - `/__ff/state` (`stock`, `orders_confirmed`, `payments_unattached`, `orders_without_payment`, `in_flight`, `build`, `stale_source`)
  - hooks enabled only when `FF_TEST_HOOKS=1`
- **Demo `CLAUDE.md`** states business rules, not the fix:
  - "a ticket sale requires a payment authorization"
  - "a customer must never be left holding an authorization without a ticket"
  - "don't remove the payment call"

**Acceptance:**
- **Bug reproduces:** 20 concurrent requests on the buggy code give more than 1 confirmation in 10/10 attempts (helper script).
- **Sequential tests pass** on the buggy code.
- **Fix holds under concurrency:** with the reference fix, 10/10 attempts give exactly 1 × 200, 19 × 409 and `payments_unattached == 0`. This also holds with `uvicorn --workers 4`, which shows correctness doesn't depend on a single event loop. Run the worker check with the helper script, not the FaultForge lifecycle: `instance_id` and `in_flight` are per process, so hooks would falsely report `target_restarted`. The demo target runs one worker.
- **Failure paths** (reference fix, integration tests):
  - decline gives 402 with stock, orders and holds unchanged
  - authorize timeout gives 503, void is called, nothing is left unattached
  - a forced exception inside the transaction gives 500, rolls back (stock unchanged, no order) and voids
  - losing the claim gives 409 and voids
  - a void failure gives `payments_unattached == 1`, visible in state
- **Sloppy-fix fixture** (conditional update without void): the integration test shows `payments_unattached == 19`.
- **False-fix fixtures:** rejecting every buyer gives `accepted == 0`, and removing the payment call gives `orders_without_payment == 1`.
- **Fingerprint:**
  - editing a function and letting uvicorn reload changes `build_id` and `instance_id`
  - restarting without an edit changes only `instance_id`
  - editing without a reload makes `stale_source` true and leaves `build_id` unchanged

### M2 — Hardened k6 runner (M) — *parallel with M1*
**Tasks:**
- the k6 option verification spike (`docs/VERIFIED.md`)
- `burst_request.js` per §4.2
- argv and environment builder per §4.9
- process group handling and deadline
- streaming filter of raw output into `requests.jsonl`, then raw deletion
- completeness detection per §4.7

**Acceptance (integration tests against small local fixture servers):**
- **409s aren't errors:** the fixed target's 19 × 409 give `rejected = 19`, `unexpected = 0`, a FaultForge error rate of 0, and a k6 `http_req_failed` rate of 0.
- **No redirects followed:** an endpoint returning `302 → http://127.0.0.1:<listener port>` is classified `unexpected`, and the listener records **0 hits**.
- **IP blocklist works:** a test-only spec targeting `10.255.255.1` is blocked by k6 (error recorded, no connection). Skipped with a recorded reason if the option isn't available.
- **Environment is sanitised:** with `K6_OUT=cloud` and `K6_CLOUD_TOKEN=x` set in the parent environment, the spawned environment and argv contain neither (unit test on the builder), and no cloud output occurs.
- **Timeouts don't leak processes:** a hanging endpoint is killed at the deadline, the trial is `incomplete: timeout`, and `pgrep -f k6` is empty afterwards.
- **Short runs are detected:** a slow endpoint exceeding `maxDuration` gives `incomplete: iterations_short` even though k6 exits 0.
- **Evidence is complete and tidy:** `requests.jsonl` line count equals the requests completed, every line has all fields, and the raw json file is gone.
- **Cardinality is bounded:** `k6-summary.json` contains no per-request tag values.

### M3a — Config, safety, budget, lifecycle, CLI run (M)
**Acceptance:**
- **Origin rejection:** each of the following base URLs gives `unsafe_target`:
  - `http://example.com`
  - `http://localhost.example.com`
  - `http://user@127.0.0.1:8000`
  - `http://127.0.0.1@evil.com`
  - `http://127.1:8000`
  - `http://2130706433:8000`
  - `http://[::1]:8000`
  - `https://127.0.0.1` without a port
  - `ftp://127.0.0.1:21`
- **Path rejection:** each of the following gives `config_invalid`:
  - `//evil.com/x`
  - `http://evil.com`
  - `/a/../b`
  - `/a%2f..`
  - `/a\b`
  - `/@evil`
  - a path containing a control character
  - `${VU}` used in a path
- **Filesystem containment:**
  - `project_dir` outside the allowed roots is rejected
  - `get_run("../../etc")` is rejected
  - a runs directory symlinked outside its root is rejected
- **Budget admission:**
  - `trials=5, iterations=500` gives `budget_exceeded` with a suggested plan, and no requests are sent
  - `trials=3, max_duration_s=20` exceeds the wall budget and is rejected at admission
- **Budget enforcement:** against a hanging target with an admitted plan, the whole call returns within `MAX_WALL_S_TOTAL + 5 s`, later trials are `skipped: budget_exhausted`, and the verdict is `INCONCLUSIVE`.
- **Restart detection:** restarting the target mid-trial gives that trial `incomplete: target_restarted`.
- **Clamping:** `vus=500` is clamped to 50 and listed in `budget.clamped`. `trials=6` is rejected with `budget_exceeded`.
- **Contention:** a concurrent trial that stays below `min_peak_in_flight` is `incomplete: insufficient_contention`. With no violation, the verdict is `INCONCLUSIVE`, never `PASS`.
- **Reset failure:** a failing reset hook gives `incomplete: reset_failed`.
- **Target down:** gives `ERROR`, `target_unreachable` and a start hint.

### M3b — Oracle, report, compare (M) — *can start on M0 fixtures in parallel with M3a*
**Acceptance:**
- **Expression whitelist:** the evaluator rejects `__import__('os')`, `f()`, `a[0]`, `before.__class__` and unknown variables.
- **Buggy code:** `FAIL` with `V: capacity_exceeded` and `V: accounting_mismatch`, both `proven`; hypotheses `check_then_act_race` and `lost_update`, both `suspected`.
- **Reference fix:** `PASS`.
- **Sloppy fix:** `FAIL` with `orphaned_resource` and hypothesis `missing_compensation`.
- **False fixes:** rejecting every buyer gives `FAIL` with `liveness_failed` and hypothesis `over_rejection`. Removing the payment call gives `FAIL` with `integrity_violated` and hypothesis `required_step_bypassed`.
- **Verdict rules:**
  - an incomplete trial with a capacity violation gives `FAIL`
  - incomplete trials without a violation give `INCONCLUSIVE`, never `PASS`
  - non-capacity invariants on incomplete trials are `not_evaluable`
- **Report size and evidence:**
  - with 2,000 requests, `report.json` is 50 KB or less and has at most 10 samples per violation
  - the `requests.jsonl` sha256 in the report matches the file
- **Percentile cross-check:** FaultForge's p95 matches k6's within 1 ms for a single trial.
- **Comparison rules:**
  - buggy run → reference-fix run gives `FIXED` for both violated invariants
  - a candidate with the same `build_id` gives `NOT_VERIFIED`
  - a different `config_hash` gives `NOT_COMPARABLE`
  - a stale candidate gives `NOT_VERIFIED`
  - a `not_evaluable` invariant on either side gives `NOT_VERIFIED`
  - a retest with a different `vus` or `trials` gives `NOT_COMPARABLE`
- **Rendering:** `report.md` has separate Proven and Suspected sections, and a completeness line next to the verdict.

### M4 — MCP server (S–M)
**Acceptance:**
- **Protocol hygiene:** MCP Inspector shows the 3 tools, and stdout carries protocol messages only.
- **Claude Code integration:** in Claude Code, `/mcp` shows FaultForge connected. A natural-language request lists the scenarios, then runs one and returns `FAIL (1/1 complete)` within 15 s.
- **Output shape:** the output is 60 lines or fewer, with separate Proven and Suspected sections.
- **Evidence paging:** `get_run(section="evidence", limit=5, offset=5)` pages correctly.
- **Structured errors:** errors come back as structured results (tested with the target down, a busy lock and an over-budget plan).

### M5 — Verification loop (S–M)
**Tasks:** trials, serial control, hypothesis upgrade, `compare_to` in `run_scenario`, and tool descriptions that nudge a rerun with `compare_to`.

**Acceptance:**
- **Scripted session:**
  1. A run with `trials=3, serial_control=true` gives `FAIL 3/3`, with hypotheses `supported`.
  2. The reference fix is applied.
  3. A rerun with `compare_to` gives `PASS (3/3 complete)` and `FIXED`.
- **Reload guard:** a rerun without reloading gives `NOT_VERIFIED (stale_source)`.
- **Inherited plan:** the rerun passes only `compare_to` and still runs 3 trials plus the serial control.
- **Timing:** the whole call takes 15 s or less.

### M6 — Demo hardening (S)
**Tasks:**
- `faultforge doctor`, which checks:
  - k6 is on PATH *as seen by the MCP server*
  - the target is reachable and hooks are enabled
  - `stale_source` is false
  - the MCP timeout setting is in place
  - no k6 process is left over
- `Makefile`: `make target`, `make bug` (restores the buggy code with git), `make doctor`
- a demo checklist
- a recorded fallback video

**Acceptance:** 10 consecutive live rehearsals each finish in 120 s or less with correct verdicts. **This is the gate for M7.**

### M7 — Claude-assisted scenario generation (SHOULD, S–M)
**Tasks:**
- `faultforge_validate_scenario`
- the authoring guide as an MCP prompt/resource (schema, invariant types, examples, and the rule that invariants are business rules Claude must confirm with the user)
- new or edited scenarios default to `user_confirmed: false`
- running an unconfirmed scenario is allowed, but the verdict carries the disclosure *"invariants not confirmed by user"*

**Acceptance:**
- **Generated scenario works:** with `faultforge.yaml` removed from `ticket-api`, Claude drafts one, presents the invariants for confirmation, validates with zero errors, and runs. The resulting violation types match the hand-written scenario.
- **Unsafe drafts are blocked:** the validator rejects fixtures with an external origin, a bad path, an unknown state variable, a missing reset on a mutating scenario, and a disallowed expression.
- **Validation is read-only:** validating never calls reset or sends load (checked by hook call counters).

**Parallel work:** M1 ∥ M2 after M0; M3b ∥ M3a on fixtures; M4 can start against a stubbed `lifecycle.run()` that returns fixture reports.

---

## 8. Demo script (~100 s)

**Pre-stage:**
- `make target`, then `make doctor` shows green
- Claude Code is open in `demo/ticket-api`, on the buggy code
- `MCP_TOOL_TIMEOUT` is set

| Time | On screen | What happens |
|---|---|---|
| 0:00 | `pytest` → green | "Ticket launch tomorrow. Tests pass." |
| 0:10 | *"We're launching ticket sales. Stress-test the reserve flow with FaultForge and tell me what's wrong."* | `list_scenarios` → `run_scenario(trials=3, serial_control=true)` |
| 0:25 | **FAIL (3/3 complete)**. **Proven:** up to ~15 tickets confirmed for 1 in stock; stock fell by 1 while ~15 orders were written. **Suspected (supported):** check-then-act race, lost update; serial control held. 409s shown as rejections, error rate 0. | |
| 0:45 | Claude reads `/reserve` | Points to the stock read → `await authorize` → write from the stale value |
| 1:00 | Claude edits | Atomic claim and order in one transaction, void on loss or failure; uvicorn reloads |
| 1:15 | *"Verify the fix."* | `run_scenario(compare_to=<run1>)` |
| 1:25 | **PASS (3/3 complete)**, both invariants **FIXED**, 1 × 200 / 19 × 409, `payments_unattached 0`, new `build_id` | |
| 1:40 | Close | |

**Bonus moment, if it happens naturally:** Claude forgets the void. The `orphan` invariant fails and Claude corrects it. Don't script this.

**Failure risks:**

| Risk | Mitigation |
|---|---|
| Server didn't reload | `stale_source` → `NOT_VERIFIED`, and the message says so |
| k6 not on the MCP server's PATH | `doctor` checks from the server's environment; absolute path in config |
| Tool timeout | Measured budget fits the timeout; `doctor` checks the setting |
| Claude removes the payment await | The `integrity` invariant fails the run; `CLAUDE.md` business rule; rehearse |
| Race doesn't reproduce on stage hardware | Rehearse on the demo machine; `FF_AUTH_DELAY_MS` |
| Network or model slowness | CLI fallback shows identical reports; recorded video |

**Disclosure to judges:** the payment provider is an in-process stub with a fixed delay. The race, the load, the state checks and the verdicts are all real.

---

## 9. Risks and tradeoffs

- **Authorize-then-claim:** a simpler fix, but losing buyers briefly hold authorizations. Documented; reserve-then-authorize is FUTURE.
- **Bytecode fingerprint:** it reflects what is actually running, at the cost of being Python-specific and changing when only line numbers move. Other stacks would need their own build reporter (FUTURE).
- **Rejecting over-budget plans** instead of shrinking them is less convenient, but keeps "verified" meaningful.
- **INCONCLUSIVE** makes some runs feel less decisive; that is the honest outcome.
- **Test hooks** (`/__ff/*`) are needed for state-based invariants and are gated by `FF_TEST_HOOKS`. Apps without hooks are limited to client-only invariants.
- **Hypotheses are pattern-matched.** They can be wrong, which is why they are labelled, tied to the evidence they rest on, and never feed the verdict.
- **Generality:** one script and one scenario demonstrate the framework. M7 is what shows it extends.

---

## 10. Open decisions

1. ~~Where is the team's existing code?~~ Resolved: there is none.
2. ~~Python or TypeScript?~~ Resolved: Python.
3. Hackathon length and team size. If under about 30 person-hours, apply the cut order in §6.
4. The demo machine's OS (avoid native Windows).
5. Is `MCP_TOOL_TIMEOUT` controllable on the demo machine? If not, lower `MAX_WALL_S_TOTAL`. *(new)*
6. Authorize-then-claim (recommended for the demo) or reserve-then-authorize? *(new)*
7. Should `serial_control` be on by default in the demo run? Recommended, since it adds about 1–2 s. *(new)*
8. Where should run artifacts live: `.faultforge/` in the target repo, gitignored (recommended)?
9. **Hackathon track and deployment (§11).** *(new, blocks submission)*

---

## 11. Hackathon compliance: Vultr Agent Rush (unresolved)

The official event page was checked on 2026-11-06. This plan's local-only design doesn't yet meet the submission rules.

| Requirement | Blast Radius Zero | Future of Work | This plan today |
|---|---|---|---|
| Backend on a Vultr VM | Required | Required | Missing |
| Web app with a public demo URL | Required | Required | Missing |
| Vultr Serverless Inference for LLM calls | Required | Optional | No LLM in FaultForge; Claude Code is the agent |
| Actions isolated in a sandbox on Vultr | Required | Not required | Missing |
| Containment moment in the demo video | Required | Not required | Missing |

Online building ends 8 November 2026. The page gives no exact deadline or time zone, so confirm both with the organisers. See §10 decision 9.
