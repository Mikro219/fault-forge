#!/usr/bin/env bash
# Builds faultforge-redeem-code:buggy, :fixed and :fixed-schema for the default-suite stack tests, through
# node_build: the lint, the install cache and the sealed build, never a docker build of project code.
#
#   scripts/build_redeem_demo.sh [--allow-network]
#
# With a warm install cache this uses no network. On a cold one it needs --allow-network: one fetch
# of the demo's lockfile from registry.npmjs.org, through the registry-only proxy.
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
allow=0
if [[ "${1:-}" == "--allow-network" ]]; then allow=1; elif [[ $# -gt 0 ]]; then echo "usage: build_redeem_demo.sh [--allow-network]" >&2; exit 2; fi

cd "$root"
FF_DEMO_ALLOW_FETCH="$allow" uv run python - <<'PY'
import os, shutil, sys, tempfile
from pathlib import Path

from faultforge import node_build
from faultforge.mcp_server.settings import private_dir, state_dir
from faultforge.stack import load_stack

demo = Path("demo/redeem-code").resolve()
build = load_stack(demo / "stack.yaml").build
state = private_dir(state_dir())
allow = os.environ["FF_DEMO_ALLOW_FETCH"] == "1"
with tempfile.TemporaryDirectory(prefix="faultforge-redeem-demo-") as temp:
    sources = {"buggy": demo}
    # Each fix is taken as verify_fix takes one: only the paths in the fix scope, over the baseline.
    for tag, overlay in {"fixed": "reference_fix", "fixed-schema": "reference_fix_schema"}.items():
        candidate = Path(temp) / f"candidate-{tag}"
        shutil.copytree(demo, candidate)
        shutil.copytree(demo / overlay, candidate, dirs_exist_ok=True)
        sources[tag] = Path(temp) / tag
        node_build.assemble_source(demo, candidate, build.fix_scope, sources[tag])
    for tag, source in sources.items():
        built = node_build.build_stack_image(source, build, state, allow_fetch=allow, tag=f"faultforge-redeem-code:{tag}")
        if built.image_id is None:
            print(f"{tag}: {built.failure_reason}: {built.failure_detail or ''}", file=sys.stderr)
            if built.failure_reason == "install_cache_missing":
                print("Run again with --allow-network to fetch the demo's dependencies once.", file=sys.stderr)
            if built.log_tail:
                print(built.log_tail, file=sys.stderr)
            sys.exit(1)
        print(f"built faultforge-redeem-code:{tag} ({built.line()}; source {built.source_sha256[:12]})")
        for line in built.proxy:
            print(f"  proxy: {line}")
PY
