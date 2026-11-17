#!/usr/bin/env bash
# Builds the Node toolchain images for a stack built from source: build_node_images.sh <node> <pnpm>.
# The one network use is `npm pack pnpm@<pnpm>` from registry.npmjs.org; the tarball's sha512 must
# match docker/pnpm.lock before it is installed. The MCP server never pulls or builds these.
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
node="${1:?usage: build_node_images.sh <node version> <pnpm version>}"
pnpm="${2:?usage: build_node_images.sh <node version> <pnpm version>}"
[[ "$node" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ && "$pnpm" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || { echo "versions must be exact, such as 24.22.3 12.21.0" >&2; exit 2; }

locked="$(sed -n 's/^version=//p' "$root/docker/pnpm.lock")"
sha512="$(sed -n 's/^sha512=//p' "$root/docker/pnpm.lock")"
if [[ "$locked" != "$pnpm" || ! "$sha512" =~ ^[0-9a-f]{128}$ ]]; then
    echo "docker/pnpm.lock has no sha512 for pnpm $pnpm; add the tarball's checksum there first" >&2
    exit 2
fi

build() {
    # No attestations, so a full cache hit keeps the image's ID, which keys the install cache.
    docker build --provenance=false --sbom=false --quiet --pull=false --file "$root/docker/node-toolchain.Dockerfile" --target "$1" \
        --build-arg "NODE_VERSION=$node" --build-arg "PNPM_VERSION=$pnpm" --build-arg "PNPM_SHA512=$sha512" \
        --tag "$2" "$root"
}
build build "faultforge-node-build:${node}-pnpm${pnpm}"
build run "faultforge-node-run:${node}"
