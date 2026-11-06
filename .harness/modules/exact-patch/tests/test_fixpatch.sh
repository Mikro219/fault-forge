#!/usr/bin/env bash
# Executable tests for scripts/fixpatch.sh (quartz-rs-p49, quartz-rs-3bm). Each
# case builds a throwaway git repository under mktemp; nothing touches a real
# checkout. Run: bash <module dir>/tests/test_fixpatch.sh
# (TMPDIR=/mnt/c/... for drvfs)
set -euo pipefail
FP="$(cd "$(dirname "$0")/.." && pwd)/scripts/fixpatch.sh"
root="$(mktemp -d)"; bg=""
trap '[[ -z "$bg" ]] || kill "$bg" 2>/dev/null; chmod -R u+rwx "$root" 2>/dev/null; rm -rf "$root"' EXIT
pass=0
ok() { echo "PASS  $1"; pass=$((pass + 1)); }
fail() { echo "FAIL  $1"; exit 1; }
repo() {
  local d="$root/$1"; mkdir -p "$d"; cd "$d"
  git init -q; git config user.email t@t; git config user.name t
  printf 'line1\nline2\nline3\nline4\nline5\n' > a.rs; echo "target/" > .gitignore
  git add -A; git commit -qm init
}
sha() { sha256sum "$1" | cut -d' ' -f1; }
tree_of() { awk '{print $NF}' <<<"$1"; }
rc_of() { local r=0; "$@" >/dev/null 2>&1 || r=$?; echo "$r"; }
recorded() { cut -f1 "$(git rev-parse --path-format=absolute --git-common-dir)/bugfix/$1.worktrees" 2>/dev/null || true; }

repo same_file
sed -i 's/line1/line1 UNRELATED user edit/' a.rs
echo "user notes" > notes.txt; git add notes.txt
before_index="$(sha .git/index)"; before_tree="$(sha256sum a.rs notes.txt)"
"$FP" baseline bug1 >/dev/null
[[ "$(sha .git/index)" == "$before_index" && "$(sha256sum a.rs notes.txt)" == "$before_tree" ]] \
  && ok "baseline leaves the real index and working tree untouched" || fail "baseline touched index or tree"
sed -i 's/line4/line4 FIX/' a.rs
mkdir -p tests; echo "regression test" > tests/regress.rs
mkdir -p target; echo "build junk" > target/out
"$FP" patch bug1 "$root/fix.patch" > "$root/patch.out"
P="$root/fix.patch"; S="$(sha "$P")"
grep -q '^+line4 FIX' "$P" && ! grep -q '^[-+].*UNRELATED' "$P" \
  && ok "unrelated edit in the same file is excluded, the fix is included" || fail "patch content wrong"
grep -q 'tests/regress.rs' "$P" && ok "untracked regression test is included" || fail "untracked test omitted"
! grep -q 'notes.txt' "$P" && ! grep -q 'target/out' "$P" \
  && ok "pre-existing staged file and ignored build output are excluded" || fail "unrelated or ignored file included"
"$FP" verify bug1 "$P" "$S" >/dev/null && ok "verify passes while nothing changed" || fail "verify false alarm"
echo "late edit" >> tests/regress.rs
if "$FP" verify bug1 "$P" "$S" >/dev/null; then fail "verify missed a change after review"; else ok "verify fails after a post-review change"; fi
sed -i '$d' tests/regress.rs
cp "$P" "$root/keep.patch"; echo "# rewritten" >> "$P"
out="$("$FP" verify bug1 "$P" "$S" || true)"
[[ "$out" == *"no longer hashes"* ]] && ok "verify fails when the reviewed patch file was rewritten" || fail "verify trusted a rewritten patch: $out"
cp "$root/keep.patch" "$P"
"$FP" materialize bug1 "$P" "$root/iso" >/dev/null
grep -q 'line4 FIX' "$root/iso/a.rs" && grep -q 'UNRELATED' "$root/iso/a.rs" && [[ -f "$root/iso/tests/regress.rs" ]] \
  && [[ -f "$root/iso/notes.txt" ]] && ok "materialize rebuilds baseline + exact patch in an isolated worktree" \
  || fail "materialized tree wrong"
[[ "$(git -C "$root/iso" rev-parse HEAD)" == "$(git rev-parse refs/bugfix/bug1^)" ]] \
  && ok "materialize starts at the baseline's HEAD" || fail "materialize base"
"$FP" cleanup bug1 "$root/iso" >/dev/null
"$FP" materialize bug1 none "$root/orig" >/dev/null
grep -q '^line4$' "$root/orig/a.rs" && grep -q 'UNRELATED' "$root/orig/a.rs" && [[ ! -e "$root/orig/tests/regress.rs" ]] \
  && ok "materialize ... none rebuilds the original code alone" || fail "baseline-only checkout wrong"
"$FP" cleanup bug1 "$root/orig" >/dev/null
"$FP" candidate bug1 "$P" "$root/cand" >/dev/null 2>&1 && fail "candidate applied over an unrelated edit in the hunk context" \
  || ok "candidate (HEAD + patch only) fails closed when unrelated work sits in the fix's hunk"
[[ ! -e "$root/cand" ]] && ! git worktree list | grep -q cand && ! recorded bug1 | grep -q cand \
  && ok "a failed worktree build leaves nothing behind, not even its record" || fail "leftover worktree"
if "$FP" patch bug1 "$PWD/inside.patch" >/dev/null 2>&1; then fail "patch written inside the checkout"; else ok "a patch path inside the checkout is refused"; fi
if "$FP" materialize bug1 none "$PWD/inside-wt" >/dev/null 2>&1; then fail "worktree inside the checkout"; else ok "a worktree inside the checkout is refused"; fi
if "$FP" baseline bug1 >/dev/null 2>&1; then fail "a second baseline silently replaced the first"; else ok "an existing baseline is never overwritten"; fi
git update-ref refs/bugfix/other "$(git rev-parse HEAD)"
"$FP" drop bug1 && ! git rev-parse -q --verify refs/bugfix/bug1 >/dev/null && git rev-parse -q --verify refs/bugfix/other >/dev/null \
  && ok "drop removes only the named baseline ref" || fail "drop"
[[ "$(sha .git/index)" == "$before_index" ]] && ok "real index still untouched at the end" || fail "index changed"

repo head_moves
echo "wip" >> a.rs
"$FP" baseline h >/dev/null
echo "fix" > fix.rs
"$FP" patch h "$root/h.patch" >/dev/null
git -c core.hooksPath=/dev/null commit -qam "someone commits mid-fix"
out="$("$FP" verify h "$root/h.patch" "$(sha "$root/h.patch")" || true)"
[[ "$out" == *"HEAD moved"* ]] && ok "verify fails when HEAD moved since the baseline" || fail "HEAD move missed: $out"

echo "other" > other.txt; git add other.txt; git -c core.hooksPath=/dev/null commit -qm "a commit bringing new content"
out="$("$FP" verify h "$root/h.patch" "$(sha "$root/h.patch")" || true)"
[[ "$out" == *"HEAD moved"* ]] && ok "verify reports HEAD moved even when the new commit brings content the tree lacked" || fail "HEAD move masked: $out"

repo wrapped
echo "wip" >> a.rs
"$FP" baseline r >/dev/null
echo "fix" > fix.rs
"$FP" patch r "$root/r.patch" >/dev/null
mkdir -p "$root/bin"; real="$(command -v git)"
printf '#!/bin/sh\nfor a in "$@"; do [ "$a" = read-tree ] && for b in "$@"; do [ "$b" = -u ] && exit 1; done; done\nexec "%s" "$@"\n' "$real" > "$root/bin/git"
chmod +x "$root/bin/git"
rc=0; PATH="$root/bin:$PATH" "$FP" materialize r none "$root/rwt" >/dev/null 2>&1 || rc=$?
[[ $rc == 1 && ! -e "$root/rwt" ]] && ! recorded r | grep -q rwt && ok "a failed read-tree is not masked: materialize fails and leaves nothing" || fail "masked read-tree (rc $rc)"
out="$("$FP" materialize r none "$root/rwt")"
[[ "$(tree_of "$out")" == "$(git rev-parse refs/bugfix/r^{tree})" ]] && ok "materialize none prints the baseline tree" || fail "materialize none tree"
for c in "patch r $root/r2.patch" "verify r $root/r.patch $(sha "$root/r.patch")" "staged $(git rev-parse HEAD^{tree})" "cleanup r"; do
  rc=0; (cd "$root/rwt" && "$FP" $c >/dev/null 2>&1) || rc=$?
  [[ $rc == 2 ]] || fail "'$c' ran from inside a bugfix worktree (rc $rc)"
done
ok "patch, verify, staged and cleanup refuse to run from inside a bugfix worktree"
printf '#!/bin/sh\n[ "$1" = -c ] && [ "$3" = worktree ] && [ "$4" = add ] && exit 128\nexec "%s" "$@"\n' "$real" > "$root/bin/git"
rc=0; PATH="$root/bin:$PATH" "$FP" candidate r none "$root/retry" >/dev/null 2>&1 || rc=$?
"$FP" candidate r none "$root/retry" >/dev/null
[[ $rc == 2 && "$(recorded r | grep -c retry)" == 1 ]] && "$FP" cleanup r "$root/retry" >/dev/null && [[ ! -e "$root/retry" ]] \
  && ok "a failed worktree add leaves no stale record, so a retry is still cleaned up" || fail "stale record after failed add (rc $rc)"
for i in 1 2 3 4 5 6; do "$FP" candidate r none "$root/par$i" >/dev/null & done; wait
[[ "$(recorded r | grep -c par)" == 6 ]] && ok "parallel worktree builds lose no record" || fail "records lost: $(recorded r | grep -c par)"
"$FP" cleanup r >/dev/null && [[ -z "$(recorded r)" ]] && ok "and cleanup removes them all" || fail "parallel cleanup"

repo candidate_clean
seq 1 20 | sed 's/^/line/' > a.rs; git commit -qam twenty
sed -i 's/^line1$/line1 UNRELATED/' a.rs
printf 'pub fn helper() {}\n' > helper.rs
"$FP" baseline c >/dev/null
sed -i 's/^line15$/line15 FIX/' a.rs
"$FP" patch c "$root/c.patch" >/dev/null
"$FP" candidate c "$root/c.patch" "$root/cwt" >/dev/null
grep -q 'line15 FIX' "$root/cwt/a.rs" && ! grep -q UNRELATED "$root/cwt/a.rs" && [[ ! -e "$root/cwt/helper.rs" ]] \
  && ok "candidate holds HEAD + the fix only, without uncommitted work the commit would not carry" || fail "candidate content"
"$FP" cleanup c "$root/cwt" >/dev/null

repo guarantee
printf '[[ -f limits.conf ]] && . ./limits.conf\necho $(( ${1} * ${FACTOR} ))\n' > count.sh
printf '[[ "$(bash count.sh)" == 0 ]] && echo "test: ok" || { echo "test: FAIL"; exit 1; }\n' > test.sh
git add -A; git commit -qm count
echo "FACTOR=2" > limits.conf
"$FP" baseline g >/dev/null
sed -i 's/\${1}/${1:-0}/' count.sh
"$FP" patch g "$root/g.patch" >/dev/null
mat="$(tree_of "$("$FP" materialize g "$root/g.patch" "$root/gmat")")"
cand="$(tree_of "$("$FP" candidate g "$root/g.patch" "$root/gcand")")"
(cd "$root/gmat" && bash test.sh >/dev/null) && ! (cd "$root/gcand" && bash test.sh >/dev/null 2>&1) \
  && [[ ! -e "$root/gcand/limits.conf" && "$mat" != "$cand" ]] \
  && ok "an unrelated uncommitted file that makes the fix pass is absent from the delivery candidate, whose gate fails" \
  || fail "delivery candidate carried the unrelated guarantee"
"$FP" check "$root/gcand" "$cand" >/dev/null && [[ "$(rc_of "$FP" check "$root/gcand" "$mat")" == 1 ]] \
  && ok "check binds evidence to the candidate tree and rejects the working-tree variant's tree" || fail "check tree binding"
echo "touched" >> "$root/gcand/count.sh"
[[ "$(rc_of "$FP" check "$root/gcand" "$cand")" == 1 ]] && ok "an edit inside the candidate worktree voids checks bound to its tree" || fail "check missed an edit in the candidate"
git -C "$root/gcand" checkout -q -- count.sh
echo "new" > "$root/gcand/stray.txt"
[[ "$(rc_of "$FP" check "$root/gcand" "$cand")" == 1 ]] && ok "an untracked file a gate leaves in the candidate also voids them" || fail "check missed an untracked file"
rm -f "$root/gcand/stray.txt"
"$FP" cleanup g >/dev/null && [[ -z "$(recorded g)" && ! -e "$root/gmat" && ! -e "$root/gcand" ]] && ! git worktree list | grep -qE "$root/g(mat|cand) " && ok "cleanup with no paths removes every worktree this bugfix recorded" || fail "cleanup all"

repo preexisting
printf 'echo broken\n' > other.sh
printf 'r=0\n[[ "$(bash other.sh)" == fine ]] || { echo "other: FAIL"; r=1; }\n[[ "$(cat fix.txt 2>/dev/null)" == fixed ]] || { echo "fix: FAIL"; r=1; }\nexit $r\n' > gate.sh
git add -A; git commit -qm pre
printf 'echo fine\n' > other.sh
"$FP" baseline e >/dev/null
echo fixed > fix.txt
"$FP" patch e "$root/e.patch" >/dev/null
gate() { (cd "$1" && bash gate.sh > "$2" 2>&1); }
"$FP" candidate e "$root/e.patch" "$root/ecand" >/dev/null
nofix="$("$FP" candidate e none "$root/ehead")"
"$FP" materialize e none "$root/edirty" >/dev/null
rc_c=0; gate "$root/ecand" "$root/ecand.log" || rc_c=$?
rc_h=0; gate "$root/ehead" "$root/ehead.log" || rc_h=$?
rc_d=0; gate "$root/edirty" "$root/edirty.log" || rc_d=$?
[[ $rc_c -ne 0 && $rc_d -ne 0 ]] && grep -q "other: FAIL" "$root/ecand.log" && ! grep -q "other: FAIL" "$root/edirty.log" \
  && ok "the dirty baseline passes the check the candidate fails, so it cannot attribute the failure" || fail "dirty baseline comparison"
[[ $rc_h -eq $rc_c && "$(grep other "$root/ehead.log")" == "$(grep other "$root/ecand.log")" ]] \
  && [[ "$(git -C "$root/ehead" rev-parse HEAD)" == "$(git -C "$root/ecand" rev-parse HEAD)" && "$(tree_of "$nofix")" == "$(git rev-parse HEAD^{tree})" ]] \
  && ok "candidate ... none (same HEAD, no fix) reproduces it: pre-existing, while the fix's own check passes" || fail "clean HEAD comparison"
grep -q "fix: FAIL" "$root/ecand.log" && fail "fix check failed in candidate" || true
"$FP" cleanup e >/dev/null

repo cleanup_safety
"$FP" baseline w >/dev/null
echo "fix" > fix.rs
"$FP" patch w "$root/w.patch" >/dev/null
"$FP" materialize w "$root/w.patch" "$root/w1" >/dev/null
"$FP" candidate w "$root/w.patch" "$root/w2" >/dev/null
"$FP" baseline z >/dev/null
"$FP" materialize z none "$root/z1" >/dev/null
git worktree add --detach -q "$root/userwt" HEAD
out="$("$FP" cleanup w "$root/userwt" "$root/z1" || true)"
[[ "$out" == *"REFUSED $root/userwt"* && "$out" == *"REFUSED $root/z1"* && -d "$root/userwt" && -d "$root/z1" ]] \
  && ok "cleanup refuses the user's worktree and another bugfix's worktree, and leaves both" || fail "cleanup removed a foreign worktree: $out"
(cd "$root/w2" && exec sleep 60) & bg=$!
sleep 0.5
out="$("$FP" cleanup w "$root/w2" || true)"
[[ "$out" == *"REFUSED $root/w2"*"running process"* && -d "$root/w2" ]] \
  && ok "cleanup refuses a worktree a running process (an active gate) is using" || fail "cleanup removed an active worktree: $out"
kill "$bg"; wait "$bg" 2>/dev/null || true; bg=""
git worktree remove --force "$root/w1"
git worktree add --detach -q "$root/w1" HEAD
out="$("$FP" cleanup w "$root/w1" || true)"
[[ "$out" == *"REFUSED $root/w1"*"owner record"* && -d "$root/w1" ]] \
  && ok "cleanup refuses a recorded path the user has since reused for a worktree of their own" || fail "cleanup removed a reused path: $out"
"$FP" cleanup w "$root/w2" >/dev/null && [[ ! -e "$root/w2" ]] && ! recorded w | grep -qx "$root/w2" \
  && ok "once idle, the recorded worktree is removed and its record dropped" || fail "cleanup of an idle recorded worktree"
[[ -d "$root/userwt" && -d "$root/w1" && -d "$root/z1" ]] && ok "and every unrelated worktree is still there" || fail "collateral removal"
"$FP" candidate w "$root/w.patch" "$root/w[3]*" >/dev/null
(cd "$root/w[3]*" && exec sleep 60) & bg=$!
sleep 0.5
out="$("$FP" cleanup w "$root/w[3]*" || true)"
[[ "$out" == *"running process"* && -d "$root/w[3]*" ]] && ok "the in-use check is not fooled by glob characters in the path" || fail "glob path: $out"
kill "$bg"; wait "$bg" 2>/dev/null || true; bg=""
"$FP" cleanup w "$root/w[3]*" >/dev/null
"$FP" candidate w "$root/w.patch" "$root/w4" >/dev/null
rm -rf "$root/w4"
out="$("$FP" cleanup w "$root/w4" || true)"
[[ "$out" == *"git worktree prune"* ]] && git worktree prune && [[ "$("$FP" cleanup w "$root/w4")" == "gone already"* ]] \
  && ok "a worktree deleted by hand is named as such, then its record is dropped after a prune" || fail "deleted worktree: $out"
[[ "$(rc_of "$FP" cleanup nosuch "$root/w2")" == 1 ]] && [[ "$("$FP" cleanup nosuch "$root/w2" || true)" == REFUSED* ]] \
  && ok "cleanup for a bugfix with no record refuses with a message, exit 1" || fail "cleanup without registry"

repo patchdir
"$FP" baseline p >/dev/null
echo "fix" > fix.rs
pd="$root/state/bugfix/p"
[[ ! -e "$root/state" ]] && "$FP" patch p "$pd/patch-1.patch" >/dev/null && [[ -s "$pd/patch-1.patch" ]] \
  && ok "the patch directory is created when its parents are absent" || fail "patch dir creation"
if [[ "$(stat -c %a "$pd")" == 700 ]]; then ok "and it is mode 700"
elif [[ "$root" == /mnt/* ]]; then echo "SKIP  patch dir mode: this filesystem ignores chmod (drvfs without metadata); covered on ext4"
else fail "patch dir mode $(stat -c %a "$pd")"; fi
s1="$(sha "$pd/patch-1.patch")"; echo "more" >> fix.rs
rc=0; "$FP" patch p "$pd/patch-1.patch" >/dev/null 2>&1 || rc=$?
[[ $rc -eq 2 && "$(sha "$pd/patch-1.patch")" == "$s1" ]] && ok "an existing patch file is never replaced" || fail "patch-1 replaced (rc $rc)"
"$FP" patch p "$pd/patch-2.patch" >/dev/null && [[ "$(sha "$pd/patch-1.patch")" == "$s1" && -s "$pd/patch-2.patch" ]] \
  && ok "the next patch gets its own file beside the kept one" || fail "patch-2"
echo "plain file" > "$root/afile"
rc=0; "$FP" patch p "$root/afile/x.patch" >/dev/null 2>&1 || rc=$?
[[ $rc -eq 2 && "$(cat "$root/afile")" == "plain file" ]] && ok "a path through a regular file fails without writing anything" || fail "invalid parent (rc $rc)"
mkdir -p "$root/adir"
rc=0; "$FP" patch p "$root/adir" >/dev/null 2>&1 || rc=$?
[[ $rc -eq 2 && -z "$(ls -A "$root/adir")" ]] && ok "an existing directory as the patch path is refused" || fail "directory as patch (rc $rc)"
[[ -z "$(find "$root/state" "$root/adir" -name '*.tmp.*')" ]] && ok "no temporary patch files are left behind" || fail "tmp leftovers"

repo staged_content
echo "unrelated wip" > wip.txt
"$FP" baseline st >/dev/null
sed -i 's/line4/line4 FIX/' a.rs
"$FP" patch st "$root/st.patch" >/dev/null
T="$(tree_of "$("$FP" candidate st "$root/st.patch" "$root/stc")")"
"$FP" cleanup st >/dev/null
cp a.rs "$root/a.keep"; sed -i 's/line4 FIX/line4 FIX but not the reviewed one/' a.rs; git add a.rs; cp "$root/a.keep" a.rs
[[ "$(git diff --cached --name-only)" == "$(git diff --name-only HEAD "$T")" ]] || fail "setup: staged names should match"
i0="$(sha .git/index)"
out="$("$FP" staged "$T" || true)"
[[ "$out" == FAIL* && "$(sha .git/index)" == "$i0" ]] \
  && ok "staged filenames match but content differs: refused, and the index is left as it was" || fail "staged content check: $out"
git restore --staged a.rs
git apply --cached "$root/st.patch"
out="$("$FP" staged "$T" || true)"
[[ "$out" == "staged tree is the validated $T" ]] && ok "the exact reviewed patch staged in its place passes" || fail "exact staging: $out"

repo stage_exact
seq 1 20 | sed 's/^/line/' > a.rs; git commit -qam twenty
sed -i 's/^line1$/line1 UNRELATED/' a.rs; echo "notes" > notes.txt
"$FP" baseline x >/dev/null
sed -i 's/^line15$/line15 FIX/' a.rs; echo "test" > regress.rs
"$FP" patch x "$root/x.patch" >/dev/null
T="$(tree_of "$("$FP" candidate x "$root/x.patch" "$root/xc")")"
"$FP" cleanup x >/dev/null
git diff --cached --quiet && git apply --cached "$root/x.patch" && "$FP" staged "$T" >/dev/null \
  && git -c core.hooksPath=/dev/null commit -qm fix && [[ "$(git rev-parse HEAD^{tree})" == "$T" ]] \
  && ok "the exact validated candidate stages, passes staged, and commits as that tree" || fail "exact candidate staging"
grep -q UNRELATED a.rs && [[ -f notes.txt ]] && ! git ls-files --error-unmatch notes.txt >/dev/null 2>&1 && git diff --quiet HEAD -- regress.rs \
  && ok "and the unrelated work stays uncommitted in the working tree" || fail "unrelated work swept in or lost"
echo "drift" > late.txt; git add late.txt; i0="$(sha .git/index)"
out="$("$FP" staged "$(git rev-parse HEAD^{tree})" || true)"
[[ "$out" == FAIL*late.txt* && "$(sha .git/index)" == "$i0" ]] && git diff --cached --name-only | grep -qx late.txt \
  && ok "unrelated staged work makes staged fail, is named, and stays staged" || fail "staged work: $out"

repo execbit
git config core.filemode false
"$FP" baseline xb >/dev/null
printf '#!/bin/sh\necho hi\n' > tool.sh; sed -i 's/line2/line2 FIX/' a.rs
"$FP" patch xb "$root/xb.patch" >/dev/null
T="$(tree_of "$("$FP" candidate xb "$root/xb.patch" "$root/xbc")")"
"$FP" cleanup xb >/dev/null
git apply --cached "$root/xb.patch" && git update-index --chmod=+x tool.sh
[[ "$(rc_of "$FP" staged "$T")" == 1 ]] && ok "an executable bit set at delivery fails staged unless its path is listed" || fail "unlisted +x accepted"
"$FP" staged "$T" tool.sh >/dev/null && ok "listing that path accepts exactly the 100644 -> 100755 change" || fail "listed +x refused"
[[ "$(rc_of "$FP" staged "$T" a.rs tool.sh)" == 1 ]] && ok "listing a path whose mode did not change fails" || fail "unchanged listed path accepted"
git show :a.rs > "$root/ax"; echo extra >> "$root/ax"; git update-index --cacheinfo "100644,$(git hash-object -w "$root/ax"),a.rs"
[[ "$(rc_of "$FP" staged "$T" tool.sh)" == 1 ]] && ok "a listed exec path never excuses a content change elsewhere" || fail "content change excused"

repo kinds
printf '\x00\x01binary' > bin.dat; echo keep > gone.txt; echo body > old.txt; git add -A; git commit -qm kinds
"$FP" baseline k >/dev/null
printf '\x00\x02binary' > bin.dat; rm gone.txt; mv old.txt new.txt
"$FP" patch k "$root/k.patch" > "$root/k.out"
"$FP" materialize k "$root/k.patch" "$root/kwt" >/dev/null
cmp -s bin.dat "$root/kwt/bin.dat" && [[ ! -e "$root/kwt/gone.txt" && -f "$root/kwt/new.txt" && ! -e "$root/kwt/old.txt" ]] \
  && ok "binary changes, deletions and renames survive the patch" || fail "binary/delete/rename"
"$FP" cleanup k >/dev/null

repo snapshot_failure
echo "wip" >> a.rs; echo "secret" > locked.txt; chmod 000 locked.txt
if [[ -r locked.txt ]]; then
  echo "SKIP  failed-snapshot case: this filesystem ignores chmod 000 (drvfs without metadata); covered on ext4"
else
  if "$FP" baseline f >/dev/null 2>&1; then fail "a failed snapshot was recorded as a baseline"; else ok "a failed snapshot exits non-zero"; fi
  ! git rev-parse -q --verify refs/bugfix/f >/dev/null && ok "and leaves no baseline ref" || fail "ref created after failure"
fi
chmod 644 locked.txt
if "$FP" verify nosuch "$root/k.patch" "$(sha "$root/k.patch")" >/dev/null 2>&1; then fail "verify passed with no baseline"; else ok "verify without a baseline fails"; fi
cp "$root/k.patch" "$root/k2.patch"
"$FP" patch nosuch "$root/k2.patch" >/dev/null 2>&1 || true
cmp -s "$root/k.patch" "$root/k2.patch" && ok "a failed patch run never truncates an existing patch file" || fail "patch file clobbered"

repo no_change
"$FP" baseline b2 >/dev/null
if "$FP" patch b2 "$root/empty.patch" >/dev/null 2>&1; then fail "empty patch accepted"; else ok "no change since baseline is reported, not an empty pass"; fi
[[ ! -e "$root/empty.patch" ]] && ok "and writes no file" || fail "empty patch file left"

for gap in 3 14; do
  repo "stage_$gap"
  seq 1 20 | sed 's/^/line/' > a.rs; git commit -qam twenty
  sed -i 's/^line1$/line1 UNRELATED/' a.rs
  "$FP" baseline s >/dev/null
  sed -i "s/^line$((1 + gap))\$/line$((1 + gap)) FIX/" a.rs
  "$FP" patch s "$root/s$gap.patch" >/dev/null
  if git apply --cached "$root/s$gap.patch" 2>/dev/null; then
    staged="$(git diff --cached | grep '^[-+]line' | tr '\n' ' ')"
    [[ "$gap" == 14 && "$staged" == "-line15 +line15 FIX " ]] && ok "fix far from unrelated edit stages alone" \
      || fail "staging with gap $gap: '$staged'"
  else
    [[ "$gap" == 3 ]] && ok "fix whose hunk context holds an unrelated edit refuses to stage (fail closed)" \
      || fail "gap $gap should have staged"
  fi
done

echo "$pass passed"
