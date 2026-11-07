#!/usr/bin/env bash
# Deploys this checkout to a provisioned VM: deploy/deploy.sh <ip>
# Run from a dev machine with SSH access as root, or set FF_DEPLOY_USER. Prints each command. No
# secret passes through here: the key file exists only on the VM.
set -euo pipefail

if [[ $# -ne 1 || ! "$1" =~ ^([0-9]{1,3}\.){3}[0-9]{1,3}$ ]]; then
    echo "usage: deploy/deploy.sh <ipv4-address>" >&2
    exit 2
fi
ip="$1"

# This copies the working tree, whatever branch it's on, so only a clean main may go live.
checkout="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
branch="$(git -C "$checkout" rev-parse --abbrev-ref HEAD)"
if [[ "$branch" != "main" ]]; then
    echo "refusing to deploy: this checkout is on '$branch', not main" >&2
    exit 2
fi
if [[ -n "$(git -C "$checkout" status --porcelain --untracked-files=normal)" ]]; then
    echo "refusing to deploy: this checkout has uncommitted changes" >&2
    exit 2
fi
hostname="${ip//./-}.sslip.io"
remote="${FF_DEPLOY_USER:-root}@${ip}"
root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

rsync_command=(
    rsync -az --delete
    --exclude .git/ --exclude .venv/ --exclude .tools/ --exclude .faultforge/
    --exclude .claude/ --exclude .pytest_cache/ --exclude __pycache__/ --exclude '*.pyc'
    --exclude plans/ --exclude docs/SUBMISSION.md
    --exclude .env --exclude '.env.*' --exclude '*.env' --exclude '*.pem' --exclude '*.key' --exclude 'id_rsa*' --exclude 'id_ed25519*'
    "$root/" "$remote:/opt/faultforge/"
)
printf '+ %s\n' "${rsync_command[*]}"
"${rsync_command[@]}"

printf '+ ssh %s (remote steps, printed as they run)\n' "$remote"
# shellcheck disable=SC2087
ssh "$remote" bash -s -- "$hostname" <<'REMOTE'
set -euo pipefail
hostname="$1"
run() {
    printf '+ %s\n' "$*"
    "$@"
}

cd /opt/faultforge
run chown -R root:root /opt/faultforge
run /usr/local/bin/uv sync --frozen --no-dev --extra web --python /usr/bin/python3
run bash scripts/build_images.sh

run install -m 0644 deploy/faultforge.service /etc/systemd/system/faultforge.service
sed "s/__HOSTNAME__/${hostname}/g" deploy/Caddyfile.template > /etc/caddy/Caddyfile
run caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile

run systemctl daemon-reload
run systemctl enable faultforge caddy
run systemctl restart faultforge
run systemctl reload-or-restart caddy

run curl -fsS --retry 10 --retry-delay 2 --retry-connrefused --max-time 60 http://127.0.0.1:8080/healthz
echo
REMOTE

printf 'Deployed: https://%s/\n' "$hostname"
printf '+ curl -fsS https://%s/healthz\n' "$hostname"
if ! curl -fsS --retry 5 --retry-delay 5 --retry-all-errors --max-time 60 "https://${hostname}/healthz"; then
    echo "warning: HTTPS isn't answering yet. Certificates can take a minute: journalctl -u caddy" >&2
    exit 1
fi
echo
