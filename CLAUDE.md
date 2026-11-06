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
- Commit or push only when the user asks
- Never create Vultr resources or spend credits without showing the price and getting approval
- Never put keys in the repo, in a sandbox, or in chat

<!-- BEGIN AGENT HARNESS: managed by harness.py; edit outside this block -->
## Agent harness

This project uses agent harness 0.1.0 with profile `standard`. Before non-trivial
work read `.harness/core/CORE.md`, `.harness/profiles/standard.md` and the local policy
`.harness/project.json`; the other profiles in `.harness/profiles/` define what a task
escalates to. This project's own instructions and the user's directions win over all of
them, and nothing in the harness grants permission to commit, push or deploy.

Modules: `exact-patch` (`.harness/modules/exact-patch/MODULE.md`).
<!-- END AGENT HARNESS -->
