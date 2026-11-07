#!/usr/bin/env bash
# Builds SportsDeck at a pinned commit into a local image, with no secrets:
#   demo/sportsdeck/build.sh <repo> <commit> <tag> [fix-dir]
# Env files are never exported into the build. Only the owner runs this: it runs the repo's install
# scripts with network access, so it's for code you trust.
set -euo pipefail
[[ $# -ge 3 ]] || { echo "usage: build.sh <repo> <commit> <tag> [fix-dir]" >&2; exit 2; }
repo="$1"; commit="$2"; tag="$3"; fix="${4:-}"
[[ "$commit" =~ ^[0-9a-f]{7,40}$ ]] || { echo "the commit must be a hash" >&2; exit 2; }
[[ "$tag" =~ ^[a-z0-9-]+$ ]] || { echo "the tag must be plain lowercase" >&2; exit 2; }

here="$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")"
if [[ -n "$fix" ]]; then fix="$(readlink -f "$fix")"; fi
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT
git clone --quiet --no-checkout "$repo" "$work/repo"
mkdir "$work/src"
git -C "$work/repo" archive "$commit" | tar -x -C "$work/src" --exclude='.env' --exclude='.env.*'
cd "$work/src"
test ! -e .env

if [[ -n "$fix" ]]; then
    python3 "$here/apply_fix.py" "$fix" .
fi
build() {
    docker build --progress=plain --label faultforge.spike=1 --tag "faultforge-sportsdeck:$tag" . >"$work/build.log" 2>&1
}
if ! build; then
    if grep -q 'can only install packages when' "$work/build.log"; then
        echo "warning: the lockfile doesn't match package.json, so dependency versions aren't pinned" >&2
        sed -i 's/^RUN npm ci$/RUN npm install --no-audit --no-fund/' Dockerfile
        build || { tail -n 20 "$work/build.log" >&2; exit 1; }
    else
        tail -n 20 "$work/build.log" >&2
        exit 1
    fi
fi
echo "built faultforge-sportsdeck:$tag from $commit"
