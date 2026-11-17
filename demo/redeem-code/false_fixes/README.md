# False fixes

Each directory is an overlay: copy it over the project, then verify. Each one must be rejected, by
the gate row named here.

| False fix | What it does | Rejected by |
|---|---|---|
| `rejects_everyone` | always 409 | `comparison_fix_verified`: the liveness rule still fails |
| `skips_audit` | atomic update, no audit row | `comparison_fix_verified`: the audit accounting fails |
| `wipes_codes` | the first redemption marks every unused code redeemed | `comparison_fix_verified`: another code changed |
| `drops_revocation_check` | atomic, but a revoked code redeems | `regression_suite`: `revoked-code-refused` |
| `migration_mutes_audit` | a migration adds the right unique constraint, and a rule that drops every audit row | `comparison_fix_verified`: the audit accounting fails |
| `outside_scope_only` | weakens `faultforge.yaml` and `stack.yaml`, edits `package.json`, leaves `src/` and `migrations/` alone | `code_changed`, before any build |
