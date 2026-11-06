# fault-forge

Bounded concurrency testing that checks business invariants, not just status codes, and returns reproducible evidence.

**Archetype:** tool
**Languages:** Python

## Commands

- **setup:** `uv sync && bash scripts/install_k6.sh`
- **test:** `uv run pytest`
- **regenerate schema:** `uv run python -m faultforge.schema`

The plan is `docs/PLAN.md`. Build one milestone at a time, then stop for review.

## Project Rules

- Follow existing code style and conventions
- Run tests before committing
- Keep commits focused and atomic
