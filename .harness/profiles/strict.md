# Profile: strict

For projects where a wrong change is expensive: shared or persistent state,
money, production data, hard-to-reverse operations. The core and every
`standard` requirement (`.harness/profiles/standard.md`) apply.

- **Plan review before code.** Non-trivial work has a written plan reviewed by
  an independent reviewer (one that did not write it: another model, a fresh
  agent with no shared context, or a person) before implementation. Review
  rounds continue until the reviewer has no open finding; a finding is closed
  by evidence or by the user's decision, never by the plan looking reasonable.
- **Owner sign-off.** The user approves the design before implementation
  starts.
- **Findings carry evidence and a severity.** File and line, command output or
  a concrete failure scenario. A finding without evidence is a question.
- **Independent post-implementation review** of the actual diff, with written
  mandates: correctness (requirements, invariants, call sites, edge cases,
  whether tests prove the claim) and failure modes (crash, restart, partial
  writes, retries, ordering, races, stale state, fail-open versus
  fail-closed). Corrections that change behavior are reviewed again.
- **Bind review to the artifact.** What was reviewed and validated is what is
  delivered; when unrelated edits share the working tree, isolate the fix (the
  `exact-patch` module does this) and record its hash with the review.
- **Live or production steps are separate.** Anything that needs a running
  system is its own approved step with pass, pending and rollback criteria.

This profile states requirements for people and agents to follow. It does not
include an automated gate that enforces them. A project that has one names it
in `retained_gates` of its local policy and keeps it.

This profile grants no permission to commit, push or deploy and never
overrides local instructions or the user.
