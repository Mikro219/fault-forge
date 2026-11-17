#!/usr/bin/env bash
# Prepares (and with --apply, makes) the change that limits the MCP server's SSH key on the runner VM
# to the one entry point (docs/MCP.md, "Restricting the server's key"). NOT applied by anything else:
# the owner decides.
#
#   scripts/restrict_runner_key.sh <server-key.pub>            prints the exact authorized_keys line
#   scripts/restrict_runner_key.sh <server-key.pub> --apply    appends it to runner's authorized_keys
#
# The server's key must be a key of its own (for example ~/.ssh/faultforge_runner_mcp): the existing
# key also logs in as root and is what deploy_runner.sh uses as runner, so restricting that one would
# stop the deploy. Applying needs the deploy key (FF_RUNNER_DEPLOY_KEY, default ~/.ssh/faultforge_vultr)
# and changes nothing but runner's own authorized_keys; the existing lines stay as they are.
set -euo pipefail
pub="${1:?usage: restrict_runner_key.sh <server-key.pub> [--apply]}"
mode="${2:-}"
read -r type blob _ < "$pub"
[[ "$type" =~ ^ssh-ed25519$|^ecdsa-sha2-nistp256$ && "$blob" =~ ^[A-Za-z0-9+/]+=*$ ]] || { echo "not an ed25519 or ecdsa public key: $pub" >&2; exit 2; }
line="restrict,command=\"/home/runner/faultforge/bin/ff-entry\" $type $blob faultforge-mcp"
echo "$line"
[[ "$mode" == --apply ]] || exit 0

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
mapfile -t cfg < <(cd "$root" && uv run --quiet python -m faultforge.mcp_server.remote config)
host="${cfg[0]}" known="${cfg[3]}"
key="${FF_RUNNER_DEPLOY_KEY:-$HOME/.ssh/faultforge_vultr}"
env -u SSH_AUTH_SOCK ssh -F /dev/null -o BatchMode=yes -o StrictHostKeyChecking=yes -o "UserKnownHostsFile=$known" \
    -o IdentitiesOnly=yes -o IdentityAgent=none -i "$key" -l runner -- "$host" \
    "test -x /home/runner/faultforge/bin/ff-entry && { grep -qxF '$line' ~/.ssh/authorized_keys || echo '$line' >> ~/.ssh/authorized_keys; } && grep -c faultforge-mcp ~/.ssh/authorized_keys"
echo "applied. Set key_path in [remote] to the matching private key."
