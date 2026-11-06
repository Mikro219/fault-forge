# Profile: standard

For ordinary maintained projects. The core applies in full.

- Non-trivial work starts from a short written plan: the change, the files it
  touches, how it will be verified. Present it and wait when it contains a
  decision that belongs to the user.
- Every bug fix comes with a test that fails without the fix, or a stated
  reason why one is impractical.
- Run the project's full checks before reporting work done; report their
  output.
- Before reporting done, review the full diff against the plan: missed call
  sites, edge cases, error paths, tests that do not prove what they claim. A
  second, independent reviewer is recommended for any change near a material
  risk named in the core.

This profile grants no permission to commit, push or deploy and never
overrides local instructions or the user. A task in a risk area named by the
core or the local policy escalates to the `strict` requirements
(`.harness/profiles/strict.md`).
