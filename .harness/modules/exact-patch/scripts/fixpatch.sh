#!/usr/bin/env bash
# Exact fix isolation for the bugfix skill (quartz-rs-p49, quartz-rs-3bm).
# Snapshots the whole working tree (staged, unstaged and untracked non-ignored
# files; the working-tree content wins) into a git tree through a temporary
# index, so the real index, the working tree, HEAD and every branch are left
# alone. The baseline is kept alive by a local ref, refs/bugfix/<name>, which
# is never pushed with branches. The fix patch is the diff between the
# baseline tree and a later snapshot, so unrelated edits that were already
# present, even in the same file, never enter it. Edits anyone makes after the
# baseline do enter it; ignored files never do.
#
# A worktree built by materialize or candidate prints its tree hash. That
# hash, with the patch sha256, is what reviews and gates are bound to: check
# proves a worktree still holds it, staged proves the real index holds it
# before a commit. Every such worktree is recorded under the repository's git
# dir (bugfix/<name>.worktrees, plus an owner nonce in the worktree's own
# admin dir), and cleanup removes only recorded, owned, idle ones.
#
# Usage (run from inside the checkout; paths are relative to the caller):
#   fixpatch.sh baseline <name>              record the pre-fix state (never drop it once edits began)
#   fixpatch.sh patch <name> <out.patch>     write the exact fix patch outside the checkout, creating
#                                            its directory (mode 700); never replaces an existing file;
#                                            prints its sha256 and changed files
#   fixpatch.sh verify <name> <patch> <sha>  exit 0 only if the current fix still equals <patch>,
#                                            <patch> still hashes to <sha>, and HEAD has not moved
#   fixpatch.sh materialize <name> <patch|none> <dir>
#                                            detached worktree at the baseline's HEAD holding the
#                                            baseline tree plus the patch (none: the original code)
#   fixpatch.sh candidate <name> <patch|none> <dir>
#                                            detached worktree at the current HEAD plus the patch
#                                            only: exactly what a commit of the fix would contain
#                                            (none: that same HEAD without the fix)
#   fixpatch.sh check <dir> <tree>           exit 0 only if the worktree <dir> still holds <tree>
#   fixpatch.sh staged <tree> [<path>...]    exit 0 only if the real index's whole tree is <tree>,
#                                            except that each listed path differs only by being
#                                            made executable (100644 -> 100755, same content);
#                                            reads a copy, never changes the index
#   fixpatch.sh cleanup <name> [<dir>...]    remove this bugfix's recorded worktrees (all, or the
#                                            listed ones); refuses unrecorded, foreign or in-use paths
#   fixpatch.sh drop <name>                  delete the baseline ref once the fix is delivered
# Exit 0 ok, 1 check failed or something refused, 2 usage or git error.
# Worktrees are created with hooks disabled.
set -euo pipefail
shopt -s inherit_errexit

die() { echo "fixpatch: $*" >&2; exit 2; }
snapshot() {
  local idx tree
  idx="$(mktemp)"
  rm -f "$idx"
  GIT_INDEX_FILE="$idx" git read-tree HEAD
  GIT_INDEX_FILE="$idx" git add -A -- . >/dev/null
  tree="$(GIT_INDEX_FILE="$idx" git write-tree)"
  rm -f "$idx"
  echo "$tree"
}
ref_of() { [[ "$1" =~ ^[A-Za-z0-9._-]+$ ]] || die "name must be [A-Za-z0-9._-]+"; echo "refs/bugfix/$1"; }
base_tree() { git rev-parse --verify -q "$(ref_of "$1")^{tree}" || die "no baseline $1; run: fixpatch.sh baseline $1"; }
diff_trees() { git diff --binary --full-index --no-ext-diff --no-textconv --no-color --src-prefix=a/ --dst-prefix=b/ "$@"; }
fold() { if [[ "$1" == /mnt/* ]]; then echo "${1,,}"; else echo "$1"; fi; }
outside() {
  local p top
  p="$(fold "$(realpath -m "$1")/")"; top="$(fold "$(pwd -P)/")"
  [[ "$p" != "$top"* ]] || die "$1 is inside the checkout; put it outside, or it enters every later snapshot"
}
registry() { echo "$(git rev-parse --path-format=absolute --git-common-dir)/bugfix/$1.worktrees"; }
owner_file() { echo "$(git -C "$1" rev-parse --absolute-git-dir)/bugfix-owner"; }
locked() {
  local fd
  exec {fd}>>"$(registry "$1").lock"
  flock "$fd"
  "${@:2}"
  exec {fd}>&-
}
append_record() { drop_record "$1" "$2"; printf '%s\t%s\n' "$2" "$3" >> "$(registry "$1")"; }
drop_record() {
  local reg tmp
  reg="$(registry "$1")"; [[ -f "$reg" ]] || return 0
  tmp="$(mktemp "$reg.XXXXXX")"
  awk -F'\t' -v d="$2" '$1 != d' "$reg" > "$tmp"
  mv -f "$tmp" "$reg"
}
deregister() { locked "$1" drop_record "$1" "$2"; }
worktree() {
  local name="$1" dir="$2" at="$3" reg nonce
  [[ ! -e "$dir" ]] || die "$dir exists"
  outside "$dir"
  reg="$(registry "$name")"; mkdir -p "$(dirname "$reg")"
  nonce="$(od -An -tx1 -N16 /dev/urandom | tr -d ' \n')"
  locked "$name" append_record "$name" "$dir" "$nonce"
  git -c core.hooksPath=/dev/null worktree add --detach -q "$dir" "$at" || { deregister "$name" "$dir"; die "git worktree add failed for $dir; nothing recorded"; }
  printf '%s %s\n' "$name" "$nonce" > "$(owner_file "$dir")"
}
in_use() {
  local links
  links="$(find /proc/[0-9]*/cwd /proc/[0-9]*/fd/ -maxdepth 1 -type l -printf '%p\t%l\n' 2>/dev/null || true)"
  awk -F'\t' -v d="$1" -v me="$$" '
    BEGIN { f = (d ~ /^\/mnt\//); if (f) d = tolower(d) }
    { split($1, p, "/"); t = f ? tolower($2) : $2
      if (p[3] != me && (t == d || index(t, d "/") == 1)) hit = 1 }
    END { exit !hit }' <<<"$links"
}
is_worktree() { git worktree list --porcelain | grep -Fxq "worktree $1"; }

cmd="${1:-}"; shift || true
git rev-parse --show-toplevel >/dev/null 2>&1 || die "not inside a git checkout"
if [[ "$cmd" != check && -f "$(git rev-parse --absolute-git-dir)/bugfix-owner" ]]; then
  die "this is a bugfix worktree; run fixpatch.sh from the user's checkout (run gates in a subshell: (cd <dir> && ...))"
fi
args=()
for i in $(seq 1 $#); do
  a="${!i}"
  if [[ ( $i -ge 2 || "$cmd" == check ) && "$a" != none && ! "$a" =~ ^[0-9a-f]{40,64}$ ]]; then args+=("$(realpath -m "$a")"); else args+=("$a"); fi
done
set -- "${args[@]+"${args[@]}"}"
cd "$(git rev-parse --show-toplevel)"
case "$cmd" in
  baseline)
    [[ $# -eq 1 ]] || die "usage: baseline <name>"
    ref="$(ref_of "$1")"
    git rev-parse --verify -q "$ref" >/dev/null && die "baseline $1 exists; never drop a baseline once edits began"
    tree="$(snapshot)"
    commit="$(git commit-tree "$tree" -p HEAD -m "bugfix baseline $1 (working tree incl. untracked)")"
    git update-ref "$ref" "$commit"
    echo "baseline $1: HEAD $(git rev-parse HEAD) tree $tree"
    ;;
  patch)
    [[ $# -eq 2 ]] || die "usage: patch <name> <out.patch>"
    outside "$2"
    [[ ! -e "$2" && ! -L "$2" ]] || die "$2 exists; every patch gets a new name and earlier ones are kept"
    base="$(base_tree "$1")"
    mkdir -p -m 700 -- "$(dirname "$2")" 2>/dev/null || die "cannot create the directory for $2"
    [[ -d "$(dirname "$2")" ]] || die "$(dirname "$2") is not a directory"
    now="$(snapshot)"
    tmp="$(mktemp "$2.tmp.XXXXXX")"
    diff_trees "$base" "$now" > "$tmp" || { rm -f "$tmp"; die "git diff failed"; }
    [[ -s "$tmp" ]] || { rm -f "$tmp"; echo "fixpatch: no change since baseline $1" >&2; exit 1; }
    ln -- "$tmp" "$2" 2>/dev/null || { rm -f "$tmp"; die "$2 appeared while writing; left it alone"; }
    rm -f "$tmp"
    echo "patch $2 sha256 $(sha256sum "$2" | cut -d' ' -f1)"
    git diff --no-ext-diff --name-status "$base" "$now"
    ;;
  verify)
    [[ $# -eq 3 ]] || die "usage: verify <name> <patch> <sha256 recorded at review>"
    base="$(base_tree "$1")"; now="$(snapshot)"
    [[ "$(sha256sum "$2" | cut -d' ' -f1)" == "$3" ]] || { echo "FAIL $2 no longer hashes to the reviewed $3"; exit 1; }
    if [[ "$(git rev-parse HEAD)" != "$(git rev-parse "$(ref_of "$1")^")" ]]; then
      echo "FAIL HEAD moved since the baseline (a commit, pull or rebase): the tree can no longer be compared with the patch; do not re-run patch (it would absorb the new commits)"; exit 1
    fi
    tmp="$(mktemp)"; diff_trees "$base" "$now" > "$tmp"
    if ! cmp -s "$tmp" "$2"; then rm -f "$tmp"; echo "FAIL the fix differs from $2: its review and validation no longer apply"; exit 1; fi
    rm -f "$tmp"
    echo "fix unchanged: $2 sha256 $3"
    ;;
  materialize|candidate)
    [[ $# -eq 3 ]] || die "usage: $cmd <name> <patch|none> <dir>"
    ref="$(ref_of "$1")"; base="$(base_tree "$1")"
    if [[ "$cmd" == materialize ]]; then at="$(git rev-parse "$ref^")"; else at="$(git rev-parse HEAD)"; fi
    worktree "$1" "$3" "$at"
    built=1
    if [[ "$cmd" == materialize ]]; then git -C "$3" read-tree -u --reset "$base" || built=0; fi
    if [[ $built == 1 && "$2" != none ]]; then git -C "$3" apply --binary --index "$2" || built=0; fi
    tree="$(git -C "$3" write-tree)" || built=0
    if [[ $built == 1 && "$2" == none ]]; then
      if [[ "$cmd" == materialize ]]; then want="$base"; else want="$(git rev-parse "$at^{tree}")"; fi
      [[ "$tree" == "$want" ]] || built=0
    fi
    if [[ $built == 0 ]]; then
      if git worktree remove --force "$3"; then
        deregister "$1" "$3"
        echo "fixpatch: could not build $3 (the patch does not apply here, or git failed); nothing was left behind" >&2
      else
        echo "fixpatch: could not build $3, and could not remove it either; it stays recorded for cleanup" >&2
      fi
      exit 1
    fi
    echo "$cmd $3 at $at tree $tree"
    ;;
  check)
    [[ $# -eq 2 && "$2" =~ ^[0-9a-f]{40,64}$ ]] || die "usage: check <dir> <tree>"
    [[ -d "$1" && "$(git -C "$1" rev-parse --show-toplevel 2>/dev/null)" == "$1" ]] || die "$1 is not a worktree root"
    now="$(cd "$1" && snapshot)"
    [[ "$now" == "$2" ]] || { echo "FAIL $1 holds tree $now, not $2: checks bound to $2 do not apply to it"; exit 1; }
    echo "unchanged: $1 tree $2 at $(git -C "$1" rev-parse HEAD)"
    ;;
  staged)
    [[ $# -ge 1 && "$1" =~ ^[0-9a-f]{40,64}$ ]] || die "usage: staged <tree> [<path>...]"
    want="$1"; shift
    top="$(pwd -P)"; exec_paths=()
    for a in "$@"; do [[ "$(fold "$a")" == "$(fold "$top")"/* ]] || die "$a is not inside the checkout"; exec_paths+=("${a:${#top}+1}"); done
    set -- "$want"
    idx="$(mktemp)"
    cp -f "$(git rev-parse --path-format=absolute --git-path index)" "$idx" 2>/dev/null || { rm -f "$idx"; die "no index to read"; }
    now="$(GIT_INDEX_FILE="$idx" git write-tree 2>/dev/null)" || { rm -f "$idx"; die "the index has unmerged entries"; }
    rm -f "$idx"
    expect="$(for x in "${exec_paths[@]+"${exec_paths[@]}"}"; do
      line="$(git ls-tree "$1" -- "$x")"
      [[ "$line" == "100644 blob "* ]] || { echo "BAD $x"; continue; }
      oid="$(cut -d' ' -f3 <<<"$line" | cut -f1)"
      printf ':100644 100755 %s %s M\t%s\n' "$oid" "$oid" "$x"
    done | sort)"
    got="$(git diff-tree -r --no-renames --abbrev=40 "$1" "$now" | sort)"
    if [[ "$got" != "$expect" || "$expect" == *BAD* ]]; then
      echo "FAIL the staged tree $now is not the validated $1 (beyond the listed executable bits); nothing was changed. Differences (validated -> staged):"
      git diff --no-ext-diff --name-status "$1" "$now"
      [[ "$expect" != *BAD* ]] || echo "listed path not a 100644 file in $1: $(grep BAD <<<"$expect" | cut -d' ' -f2- | tr '\n' ' ')"
      exit 1
    fi
    echo "staged tree is the validated $1${exec_paths[*]+ plus +x on ${exec_paths[*]}}"
    ;;
  cleanup)
    [[ $# -ge 1 ]] || die "usage: cleanup <name> [<dir>...]"
    name="$1"; shift; ref_of "$name" >/dev/null
    reg="$(registry "$name")"
    mapfile -t recorded < <(cut -f1 "$reg" 2>/dev/null)
    if [[ $# -eq 0 ]]; then set -- "${recorded[@]+"${recorded[@]}"}"; fi
    rc=0
    for d in "$@"; do
      nonce=""; [[ ! -f "$reg" ]] || nonce="$(awk -F'\t' -v d="$d" '$1 == d {print $2; exit}' "$reg")"
      if [[ -z "$nonce" ]]; then echo "REFUSED $d: not a worktree this bugfix ($name) created"; rc=1; continue; fi
      if ! is_worktree "$d"; then
        if [[ -e "$d" ]]; then echo "REFUSED $d: exists but is not a worktree of this repository"; rc=1; continue; fi
        deregister "$name" "$d"; echo "gone already: $d (record dropped)"; continue
      fi
      if [[ ! -e "$d" ]]; then echo "REFUSED $d: deleted by hand but still registered with git; run git worktree prune, then cleanup again"; rc=1; continue; fi
      if [[ "$(cat "$(owner_file "$d")" 2>/dev/null)" != "$name $nonce" ]]; then
        echo "REFUSED $d: its owner record does not match bugfix $name"; rc=1; continue
      fi
      if in_use "$d"; then echo "REFUSED $d: a running process is using it (a gate may be active)"; rc=1; continue; fi
      if git worktree remove --force "$d"; then deregister "$name" "$d"; echo "removed $d"; else echo "REFUSED $d: git worktree remove failed (locked?)"; rc=1; fi
    done
    exit "$rc"
    ;;
  drop)
    [[ $# -eq 1 ]] || die "usage: drop <name>"
    git update-ref -d "$(ref_of "$1")"
    ;;
  *) die "usage: fixpatch.sh baseline|patch|verify|materialize|candidate|check|staged|cleanup|drop ... (see the file header)" ;;
esac
