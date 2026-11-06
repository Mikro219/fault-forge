# Module: exact-patch

Isolates the exact change made for one fix from everything else in a working
tree, so that review, validation and delivery are bound to the same bytes.
Unrelated edits already present, even in the same file, never enter the
patch.

Script: `.harness/modules/exact-patch/scripts/fixpatch.sh` (run from inside
the checkout). Its file header is the full command reference. The usual flow:

1. `fixpatch.sh baseline <name>` before anyone edits; it records the whole
   working tree in a local ref `refs/bugfix/<name>` without touching the
   index, the tree or any branch.
2. Make the fix and its test.
3. `fixpatch.sh patch <name> <dir outside the checkout>/fix-1.patch`; record
   the printed sha256. Every later patch gets a new name.
4. `fixpatch.sh candidate <name> <patch> <dir outside the checkout>` builds
   HEAD plus the patch only (what a commit would contain); `materialize`
   builds baseline plus the patch. Review and validate there and record the
   printed tree hash with the patch hash.
5. `fixpatch.sh verify <name> <patch> <sha>` before delivery;
   `fixpatch.sh staged <tree>` before a commit proves the index holds the
   validated tree.
6. `fixpatch.sh cleanup <name>` removes only the worktrees it created;
   `fixpatch.sh drop <name>` deletes the baseline ref after delivery.

When to use it: the `strict` profile recommends it for any fix delivered
while other edits share the tree; it is optional otherwise. It never commits,
stages, pushes or resets anything.

Requirements: bash 4.4 or newer, git with `worktree`, `flock` (util-linux),
`sha256sum`, `realpath`, `od`, `awk`, and a Linux `/proc` (used to refuse
removing a worktree a process is using; it cannot see processes outside this
Linux system, such as Windows programs started from WSL). Paths under `/mnt/`
are compared case-insensitively for WSL drive mounts.

Tests: `bash .harness/modules/exact-patch/tests/test_fixpatch.sh` builds
throwaway repositories under `mktemp` and touches nothing else. On a WSL drive
mount set `TMPDIR` to a directory on that mount.

Provenance: copied from an earlier project's bugfix tooling with one message
changed (recorded in the harness's `provenance.json`); the file headers keep
the original issue references.
