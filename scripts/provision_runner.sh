#!/usr/bin/env bash
# Prepares the dedicated FaultForge runner VM for the remote runtime (docs/PLAN.md V4.6, M12).
#
#   scripts/provision_runner.sh <ip> [allow_ssh_from]
#
# Runs from this machine over SSH as root, with the pinned host key only. It is safe to run again.
# What it does on the VM, in order:
#   1. apt upgrade, then Docker from download.docker.com, its signing key checked by fingerprint.
#   2. A non-root `runner` user in the docker group, with the same SSH key as root. The VM is
#      dedicated to FaultForge and holds no secrets, so docker-group access there is acceptable.
#   3. SSH: keys only (no passwords anywhere), root by key only. Checked with `sshd -t` first, and
#      a new login as runner is proven before the script ends.
#   4. ufw: SSH only, from allow_ssh_from if given (default: anywhere).
#   5. Docker: log rotation, live-restore, no IPv6.
# It never prints a key, installs nothing of the project's, and creates no Vultr resources.
set -euo pipefail

ip="${1:?usage: provision_runner.sh <ip> [allow_ssh_from]}"
allow_from="${2:-any}"
key="${FF_RUNNER_KEY:-$HOME/.ssh/faultforge_vultr}"
known="${FF_RUNNER_KNOWN_HOSTS:-$HOME/.config/faultforge/runner_known_hosts}"
[[ "$ip" =~ ^[0-9]{1,3}(\.[0-9]{1,3}){3}$ ]] || { echo "not an IPv4 address: $ip" >&2; exit 2; }
[[ "$allow_from" == any || "$allow_from" =~ ^[0-9]{1,3}(\.[0-9]{1,3}){3}(/[0-9]{1,2})?$ ]] || { echo "bad allow_ssh_from: $allow_from" >&2; exit 2; }
[[ -f "$known" && -f "$key" ]] || { echo "need the pinned host key $known and the key $key" >&2; exit 2; }

ssh_opts=(-o BatchMode=yes -o StrictHostKeyChecking=yes -o "UserKnownHostsFile=$known" -o ConnectTimeout=15 -i "$key")

# The whole remote part is one script on stdin, so nothing of it is in a process list's argv.
ssh "${ssh_opts[@]}" "root@$ip" "ALLOW_FROM='$allow_from' bash -s" <<'REMOTE'
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive

echo "== 1. packages and Docker"
apt-get update -q
apt-get upgrade -y -q
apt-get install -y -q ca-certificates curl gnupg
install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /tmp/docker.asc
# Docker's published release-signing key fingerprint.
expected="9DC858229FC7DD38854AE2D88D81803C0EBFCD88"
actual="$(gpg --show-keys --with-colons /tmp/docker.asc | awk -F: '/^fpr/ {print $10; exit}')"
[[ "$actual" == "$expected" ]] || { echo "Docker's key fingerprint is $actual, not $expected" >&2; exit 1; }
install -m 0644 /tmp/docker.asc /etc/apt/keyrings/docker.asc
. /etc/os-release
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu ${VERSION_CODENAME} stable" \
    > /etc/apt/sources.list.d/docker.list
apt-get update -q
apt-get install -y -q docker-ce docker-ce-cli containerd.io docker-buildx-plugin
install -m 0644 /dev/stdin /etc/docker/daemon.json <<'JSON'
{
  "log-driver": "local",
  "log-opts": {"max-size": "10m", "max-file": "3"},
  "live-restore": true,
  "ipv6": false
}
JSON
systemctl enable --now docker
systemctl restart docker
docker version --format 'docker {{.Server.Version}}'

echo "== 2. the runner user"
id runner >/dev/null 2>&1 || useradd --create-home --shell /bin/bash runner
usermod -aG docker runner
install -d -m 0700 -o runner -g runner /home/runner/.ssh /home/runner/faultforge /home/runner/faultforge/snapshots
install -m 0600 -o runner -g runner /root/.ssh/authorized_keys /home/runner/.ssh/authorized_keys

echo "== 3. SSH: keys only"
# 10- sorts before cloud-init's 50- file, and sshd keeps the first value it reads.
install -m 0644 /dev/stdin /etc/ssh/sshd_config.d/10-faultforge.conf <<'SSHD'
PasswordAuthentication no
KbdInteractiveAuthentication no
PermitRootLogin prohibit-password
PermitEmptyPasswords no
X11Forwarding no
AllowUsers root runner
SSHD
sshd -t
systemctl reload ssh
sshd -T | grep -E '^(passwordauthentication|permitrootlogin|kbdinteractiveauthentication) '

echo "== 4. firewall"
ufw default deny incoming >/dev/null
ufw default allow outgoing >/dev/null
if [[ "$ALLOW_FROM" == any ]]; then
    ufw allow 22/tcp >/dev/null
else
    ufw allow from "$ALLOW_FROM" to any port 22 proto tcp >/dev/null
    ufw delete allow 22/tcp >/dev/null 2>&1 || true
fi
ufw --force enable >/dev/null
ufw status | sed -n '1,8p'
REMOTE

echo "== 5. a new login as runner, and docker as runner"
ssh "${ssh_opts[@]}" "runner@$ip" 'id; docker info --format "docker {{.ServerVersion}}, {{.Driver}}, cgroup {{.CgroupVersion}}"; df -h /home/runner | tail -1'
echo "provisioned $ip"
