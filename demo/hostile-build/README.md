# hostile-build: an intentional test fixture

This is deliberately misbehaving code. It is the containment target for FaultForge's own sandbox (`docs/PLAN.md` V3.6), and it never targets third parties.

**Don't run it outside a limited container.** It tries to exhaust memory and processes. A guard refuses every attack unless `FF_HOSTILE_ALLOWED=1` is set, `/.dockerenv` exists and `/proc/net/route` has no routes. It checks at import, before the attacks and before each child process.

## What it does

`POST /reserve` starts the attacks once in a background thread and never responds, so the verdict is `INCONCLUSIVE`, never `PASS`. The hooks keep answering.

Each attempt prints one line to stdout:

```
FF_PROBE {"probe": "egress_external", "phase": "result", "outcome": "blocked", "detail": "connect failed: ENETUNREACH"}
```

`started` lines come before each attempt and carry the outcome `inconclusive`. A `started` line with no matching `result` means the process was killed during that attempt.

| Probe | Attempt |
|---|---|
| `egress_external` | Bare TCP connect to 1.1.1.1:443, sending nothing |
| `egress_dns` | Resolve example.com |
| `secrets_env` | Look for secret-like variable names; reports names, never values |
| `docker_socket` | Check whether `/var/run/docker.sock` exists |
| `write_outside_tmp` | Create a file in `/app`, `/etc` and `/usr` |
| `privileges` | Read `CapEff` and try `setuid(0)` |
| `memory` | Allocate up to 4 GB in a child process |
| `fork` | Spawn up to 2000 sleeping children, then reap them |
| `cpu_spin` | Spin in a child process for 10 s |

## Limits it keeps to itself

It sends no data anywhere, destroys nothing outside its own container, uses no exploits and doesn't try to break out of the kernel or runtime. Every attempt has a timeout or cap, so it ends by itself even with no sandbox kill.

`blocked` comes from an observed error. `cpu_spin` can't be blocked by `--cpus` alone when it uses one core, so it can honestly report `not_blocked`. The sandbox's wall-clock kill is what ends a build that outlives its budget.
