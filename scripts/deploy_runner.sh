#!/usr/bin/env bash
# Deploys FaultForge to the runner VM for the remote runtime (docs/PLAN.md V4.6, M12; docs/MCP.md).
#
#   scripts/deploy_runner.sh <commit>      a commit, such as HEAD
#   scripts/deploy_runner.sh --worktree    the working tree's tracked and untracked, non-ignored files, as a
#                                          git tree written through a temporary index: no commit, no ref
#
# The owner or the orchestrator runs it; the MCP server never does. Host, user and known_hosts come from
# the [remote] section of mcp.toml. The deploy logs in with the deploy key (FF_RUNNER_DEPLOY_KEY, default
# ~/.ssh/faultforge_vultr), which may differ from the server's key_path once that one is restricted to the
# entry point (scripts/restrict_runner_key.sh). SSH uses the pinned host key only. It never prints a key,
# sends no credential to the VM and creates no Vultr resources. Safe to run again. In order:
#   0. Every path the tree would hold is checked with snapshot.is_secret, before git writes anything.
#   1. `git archive` of the tree, unpacked to /home/runner/faultforge/app/<name> (the commit or tree id).
#   2. FaultForge's wheel, built here from that same tree, so no build backend is fetched on the VM.
#   3. uv from the release pinned in uv-release.lock, its sha256 checked on the VM; then the locked,
#      hash-checked dependencies with `uv sync --frozen --no-dev --no-install-project --no-build` (wheels
#      only, from PyPI) and the wheel itself, offline, with the VM's own Python.
#   4. The forced-command entry (bin/ff-entry) and a cron line, as runner, that removes request
#      directories older than two hours under the fixed snapshots path.
#   5. The sandbox images, by `docker save | ssh docker load`, only those whose ID differs, then every ID
#      compared with this machine's. Nothing is built or pulled on the VM.
#   6. The deploy record in the MCP state directory, from what the VM itself reports.
#   7. Older app directories on the VM are removed.
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$root"
target="${1:?usage: deploy_runner.sh <commit> | --worktree}"
base=/home/runner/faultforge

mapfile -t cfg < <(uv run --quiet python -m faultforge.mcp_server.remote config)
[[ ${#cfg[@]} -eq 4 ]] || { echo "no usable [remote] section in mcp.toml" >&2; exit 2; }
host="${cfg[0]}" user="${cfg[1]}" known="${cfg[3]}"
key="${FF_RUNNER_DEPLOY_KEY:-$HOME/.ssh/faultforge_vultr}"
[[ "$host" =~ ^[a-z0-9.-]+$ && "$user" =~ ^[a-z_][a-z0-9_-]*$ ]] || { echo "bad host or user in [remote]" >&2; exit 2; }
[[ -f "$key" && -f "$known" ]] || { echo "need the deploy key $key and the pinned known_hosts file named in [remote]" >&2; exit 2; }

uv_version="$(sed -n 's/^version=//p' "$root/uv-release.lock")"
uv_sha256="$(sed -n 's/^sha256=//p' "$root/uv-release.lock")"
[[ "$uv_version" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ && "$uv_sha256" =~ ^[0-9a-f]{64}$ ]] || { echo "uv-release.lock is malformed" >&2; exit 2; }

ssh_opts=(-F /dev/null -o BatchMode=yes -o StrictHostKeyChecking=yes -o "UserKnownHostsFile=$known" -o GlobalKnownHostsFile=/dev/null
          -o UpdateHostKeys=no -o IdentitiesOnly=yes -o IdentityAgent=none -o ForwardAgent=no -o ConnectTimeout=15
          -o ServerAliveInterval=15 -o ServerAliveCountMax=4 -o LogLevel=ERROR -i "$key" -l "$user")
# Each remote command below is a fixed string; the only values in it are checked hex names, the pinned
# uv version and hash, and image names checked against a plain pattern.
rssh() { env -u SSH_AUTH_SOCK ssh "${ssh_opts[@]}" -- "$host" "$1"; }

scratch="$(mktemp -d)"
trap 'rm -rf "$scratch"' EXIT
secrets_check() {
    # The same rule a project snapshot uses (snapshot.is_secret, any path component), on the names alone.
    if ! uv run --quiet python -m faultforge.mcp_server.remote secrets; then
        echo "refusing to deploy: the paths above look like secrets (snapshot.is_secret)" >&2; exit 2
    fi
}

echo "== 0. secret-looking files"
if [[ "$target" == --worktree ]]; then
    # Listed first, so nothing is written to git's object store when this refuses.
    git ls-files -z --cached --others --exclude-standard | secrets_check
    GIT_INDEX_FILE="$scratch/index" git add -A
    tree="$(GIT_INDEX_FILE="$scratch/index" git write-tree)"
    name="$tree" commit=-
    echo "working tree as tree $tree (no commit made)"
else
    commit="$(git rev-parse --verify --quiet "${target}^{commit}")" || { echo "not a commit: $target" >&2; exit 2; }
    git ls-tree -r -z --name-only "$commit" | secrets_check
    tree="$commit" name="$commit"
    echo "commit $commit"
fi
echo "none found"

echo "== 1. the code"
[[ "$name" =~ ^[0-9a-f]{40}([0-9a-f]{24})?$ ]] || { echo "unexpected object name $name" >&2; exit 2; }
app="$base/app/$name"
git archive --format=tar "$tree" | rssh "set -e; rm -rf $app.tmp; mkdir -p -m 0700 $base/app $app.tmp; tar -x -C $app.tmp; rm -rf $app; mv $app.tmp $app"

echo "== 2. the wheel, built here from the same tree"
mkdir "$scratch/src" "$scratch/dist"
git archive --format=tar "$tree" | tar -x -C "$scratch/src"
uv build --quiet --wheel --out-dir "$scratch/dist" "$scratch/src"
wheels=("$scratch"/dist/*.whl)
[[ ${#wheels[@]} -eq 1 ]] || { echo "expected one wheel" >&2; exit 1; }
wheel="$(basename "${wheels[0]}")"
[[ "$wheel" =~ ^faultforge-[0-9][0-9a-z.]*-py3-none-any\.whl$ ]] || { echo "unexpected wheel name $wheel" >&2; exit 1; }
rssh "mkdir -p -m 0700 $app/dist && cat > $app/dist/$wheel" < "${wheels[0]}"
echo "$wheel ($(sha256sum "${wheels[0]}" | cut -c1-16))"

echo "== 3. uv $uv_version and the Python environment"
tools="$base/tools/uv-$uv_version"
rssh "set -e; if [ ! -x $tools/uv ]; then rm -rf $tools.dl; mkdir -p $tools.dl;
  curl -fsSL --retry 2 --max-time 120 -o $tools.dl/uv.tar.gz https://github.com/astral-sh/uv/releases/download/$uv_version/uv-x86_64-unknown-linux-gnu.tar.gz;
  echo '$uv_sha256  $tools.dl/uv.tar.gz' | sha256sum --check --quiet -;
  tar -xzf $tools.dl/uv.tar.gz -C $tools.dl; mkdir -p $tools; install -m 0755 $tools.dl/uv-x86_64-unknown-linux-gnu/uv $tools/uv; rm -rf $tools.dl; fi;
  $tools/uv --version"
rssh "set -e; cd $app; export UV_PYTHON_DOWNLOADS=never UV_NO_CONFIG=1;
  $tools/uv sync --frozen --no-dev --no-install-project --no-build --python /usr/bin/python3 --quiet;
  $tools/uv pip install --quiet --python $app/.venv/bin/python --no-deps --no-index --no-build $app/dist/$wheel;
  $app/.venv/bin/python --version"

echo "== 4. the forced-command entry and the snapshot reaper"
rssh "set -e; mkdir -p -m 0700 $base/bin; cat > $base/bin/ff-entry.tmp; chmod 0755 $base/bin/ff-entry.tmp; mv $base/bin/ff-entry.tmp $base/bin/ff-entry" < "$root/scripts/runner_entry.sh"
rssh "bash -s" <<'CRON'
set -e
line='*/15 * * * * find /home/runner/faultforge/snapshots -mindepth 1 -maxdepth 1 -type d -regextype posix-extended -regex ".*/[0-9a-f]{16}" -mmin +120 -exec rm -rf {} + # faultforge-reaper'
{ crontab -l 2>/dev/null | grep -v 'faultforge-reaper' || true; echo "$line"; } | crontab -
echo "cron lines: $(crontab -l | grep -c faultforge-reaper); cron $(systemctl is-active cron)"
CRON

echo "== 5. images"
images=(faultforge-target:dev faultforge-runner:dev faultforge-stack-hooks:dev faultforge-egress-proxy:dev)
while read -r image; do images+=("$image"); done < <(docker image ls --format '{{.Repository}}:{{.Tag}}' | grep -E '^faultforge-node-(build|run):[0-9][0-9A-Za-z.-]*$' | sort)
for image in ${FF_RUNNER_STOCK_IMAGES:-postgres:16-alpine redis:7-alpine}; do
    docker image inspect "$image" >/dev/null 2>&1 && images+=("$image")
done
send=()
for image in "${images[@]}"; do
    [[ "$image" =~ ^[a-z0-9][a-z0-9._/-]*:[A-Za-z0-9._-]+$ ]] || { echo "bad image name $image" >&2; exit 2; }
    mine="$(docker image inspect --format '{{.Id}}' "$image")"
    theirs="$(rssh "docker image inspect --format '{{.Id}}' $image 2>/dev/null || true")"
    if [[ "$mine" == "$theirs" ]]; then echo "same   $image ${mine:7:12}"; else echo "send   $image ${mine:7:12}"; send+=("$image"); fi
done
if [[ ${#send[@]} -gt 0 ]]; then
    started=$SECONDS
    docker save "${send[@]}" | gzip -1 | rssh "gunzip | docker load -q"
    echo "sent ${#send[@]} images in $((SECONDS - started)) s"
fi
for image in "${images[@]}"; do
    mine="$(docker image inspect --format '{{.Id}}' "$image")"
    theirs="$(rssh "docker image inspect --format '{{.Id}}' $image")"
    [[ "$mine" == "$theirs" ]] || { echo "image $image has ID ${theirs:-none} on the VM, ${mine} here" >&2; exit 1; }
done
echo "every image ID matches"

echo "== 6. the deploy record"
uv run --quiet python -m faultforge.mcp_server.remote record "$name" "$commit" "$uv_version" "${images[@]}"

echo "== 7. older app directories"
rssh "find $base/app -mindepth 1 -maxdepth 1 -type d -regextype posix-extended -regex '.*/[0-9a-f]{40}([0-9a-f]{24})?(\.tmp)?' ! -name $name -exec rm -rf {} +; ls $base/app"
echo "deployed $name to $host"
