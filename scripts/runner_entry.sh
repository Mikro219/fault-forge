#!/bin/bash
# The forced command for the MCP server's own key on the runner VM (docs/MCP.md, "Restricting the
# server's key"). deploy_runner.sh installs it at /home/runner/faultforge/bin/ff-entry; the key's
# authorized_keys line is `restrict,command="/home/runner/faultforge/bin/ff-entry" <key>`.
#
# It runs one thing: an app directory's faultforge.remote_run, exactly as the server asks for it. The
# requested command is matched whole against a fixed pattern and never handed to a shell; anything
# else gets a refusal in the entry point's own answer format.
set -euo pipefail
requested="${SSH_ORIGINAL_COMMAND:-}"
pattern='^/home/runner/faultforge/app/([0-9a-f]{40}|[0-9a-f]{64})/\.venv/bin/python -I -m faultforge\.remote_run$'
if [[ "$requested" =~ $pattern ]]; then
    exec "/home/runner/faultforge/app/${BASH_REMATCH[1]}/.venv/bin/python" -I -m faultforge.remote_run
fi
echo '{"ok":false,"error":"this key runs only faultforge.remote_run","tar_bytes":0}'
exit 1
