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

## Still to verify

- `MCP_TOOL_TIMEOUT`: not relevant while MCP is cut from the MVP.
- Behaviour inside the sandbox, including with `--read-only` and `--pids-limit`: M4.
