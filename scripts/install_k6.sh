#!/usr/bin/env bash
# Installs the k6 release pinned in k6.lock to .tools/bin/k6, verifying its checksum.
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
version="$(sed -n 's/^version=//p' "$root/k6.lock")"
sha256="$(sed -n 's/^sha256=//p' "$root/k6.lock")"
name="k6-v${version}-linux-amd64"
url="https://github.com/grafana/k6/releases/download/v${version}/${name}.tar.gz"

if [[ "$(uname -s)-$(uname -m)" != "Linux-x86_64" ]]; then
    echo "error: only linux-amd64 is supported, found $(uname -s)-$(uname -m)" >&2
    exit 1
fi

work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT

curl -fsSL --retry 2 --max-time 120 -o "$work/k6.tar.gz" "$url"
echo "${sha256}  $work/k6.tar.gz" | sha256sum --check --quiet -
tar -xzf "$work/k6.tar.gz" -C "$work" "${name}/k6"

install -D -m 0755 "$work/${name}/k6" "$root/.tools/bin/k6"
"$root/.tools/bin/k6" version
