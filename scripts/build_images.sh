#!/usr/bin/env bash
# Builds the sandbox images. k6 comes from install_k6.sh, which verifies its checksum.
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
version="$(sed -n 's/^version=//p' "$root/k6.lock")"

if ! "$root/.tools/bin/k6" version 2>/dev/null | grep -qF "v${version}"; then
    bash "$root/scripts/install_k6.sh"
fi

# No provenance or SBOM attestation: each one records when the build ran, so with it an image's ID
# changes on every run, even when every layer came from the cache. The install cache of a stack
# built from source is keyed on the toolchain image's ID, and would go cold each time.
stable=(--provenance=false --sbom=false)
docker build "${stable[@]}" --quiet --file "$root/docker/target.Dockerfile" --tag faultforge-target:dev "$root"
docker build "${stable[@]}" --quiet --file "$root/docker/runner.Dockerfile" --tag faultforge-runner:dev "$root"
docker build "${stable[@]}" --quiet --file "$root/docker/stack-hooks.Dockerfile" --tag faultforge-stack-hooks:dev "$root"
# For a stack built from source (M11): the Node toolchain and the registry-only proxy.
docker build "${stable[@]}" --quiet --file "$root/docker/egress-proxy.Dockerfile" --tag faultforge-egress-proxy:dev "$root"
bash "$root/scripts/build_node_images.sh" 24.22.3 12.21.0
