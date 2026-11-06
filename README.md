# FaultForge

Bounded concurrency testing that checks business invariants, not just status codes, and returns reproducible evidence.

Status: M0 (contracts). The runner, demo target and MCP server don't exist yet. See [docs/PLAN.md](docs/PLAN.md).

## Setup

```sh
uv sync
bash scripts/install_k6.sh
uv run pytest
```

`install_k6.sh` installs the version pinned in `k6.lock` to `.tools/bin/k6` after verifying its checksum.

## Regenerating the report schema

The pydantic models in `src/faultforge/models.py` are the source of truth. After changing them, run:

```sh
uv run python -m faultforge.schema
```
