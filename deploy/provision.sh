#!/usr/bin/env bash
# Prepares a fresh Ubuntu 24.04 VM for FaultForge. Run as root. Safe to run again.
#
# Membership of the docker group is root-equivalent, so the faultforge user can effectively do
# anything on this machine. That's why FaultForge is the only thing on the VM and the app takes
# no free-form input. Nothing else belongs here.
set -euo pipefail

UV_VERSION="0.12.18"

if [[ "$(id -u)" != "0" ]]; then
    echo "error: run as root" >&2
    exit 1
fi
# shellcheck disable=SC1091
. /etc/os-release
if [[ "${ID:-}" != "ubuntu" || "${VERSION_ID:-}" != "24.04" ]]; then
    echo "error: expected Ubuntu 24.04, found ${ID:-unknown} ${VERSION_ID:-unknown}" >&2
    exit 1
fi

export DEBIAN_FRONTEND=noninteractive

run() {
    printf '+ %s\n' "$*"
    "$@"
}

run apt-get update -qq
run apt-get install -y -qq ca-certificates curl gnupg rsync ufw python3 apparmor unattended-upgrades \
    debian-keyring debian-archive-keyring apt-transport-https

# Docker CE, from Docker's official repository
install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
chmod 0644 /etc/apt/keyrings/docker.asc
cat > /etc/apt/sources.list.d/docker.list <<LIST
deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu ${VERSION_CODENAME} stable
LIST

# Caddy, from its official repository
curl -fsSL https://dl.cloudsmith.io/public/caddy/stable/gpg.key | gpg --dearmor --yes -o /etc/apt/keyrings/caddy.gpg
chmod 0644 /etc/apt/keyrings/caddy.gpg
cat > /etc/apt/sources.list.d/caddy.list <<LIST
deb [signed-by=/etc/apt/keyrings/caddy.gpg] https://dl.cloudsmith.io/public/caddy/stable/deb/debian any-version main
LIST

run apt-get update -qq
run apt-get install -y -qq docker-ce docker-ce-cli containerd.io docker-buildx-plugin caddy

# uv, a pinned version
if ! /usr/local/bin/uv --version 2>/dev/null | grep -qF "uv ${UV_VERSION}"; then
    curl -fsSL "https://astral.sh/uv/${UV_VERSION}/install.sh" | env UV_INSTALL_DIR=/usr/local/bin UV_NO_MODIFY_PATH=1 sh
fi

# The app's user: not root, no login. Its home holds only the app's state.
if ! id faultforge >/dev/null 2>&1; then
    run useradd --system --user-group --home-dir /var/lib/faultforge --no-create-home --shell /usr/sbin/nologin faultforge
fi
run usermod -aG docker faultforge
install -d -m 0750 -o faultforge -g faultforge /etc/faultforge
install -d -m 0755 -o root -g root /opt/faultforge

# Logs: containers, then the journal, which holds the app's and Caddy's logs.
install -d -m 0755 /etc/docker
cat > /etc/docker/daemon.json <<'JSON'
{
  "log-driver": "json-file",
  "log-opts": { "max-size": "10m", "max-file": "3" }
}
JSON
install -d -m 0755 /etc/systemd/journald.conf.d
cat > /etc/systemd/journald.conf.d/faultforge.conf <<'CONF'
[Journal]
SystemMaxUse=200M
MaxRetentionSec=14day
CONF
run systemctl restart systemd-journald
run systemctl enable --now docker
run systemctl restart docker

# SSH: keys only. The file sorts first, so it wins over anything cloud-init writes. Run only if a key
# is installed, because Vultr's web console is then the only other way in.
if [[ -s /root/.ssh/authorized_keys ]]; then
    cat > /etc/ssh/sshd_config.d/00-faultforge.conf <<'CONF'
PasswordAuthentication no
KbdInteractiveAuthentication no
PermitRootLogin prohibit-password
CONF
    run sshd -t
    run systemctl reload ssh
fi

# Security updates
cat > /etc/apt/apt.conf.d/20auto-upgrades <<'CONF'
APT::Periodic::Update-Package-Lists "1";
APT::Periodic::Unattended-Upgrade "1";
CONF
run systemctl enable --now unattended-upgrades

# Firewall: SSH is allowed before enabling, so this can't lock out the current session. Sandboxes
# publish no ports, so Docker's habit of bypassing UFW for published ports doesn't apply.
run ufw default deny incoming
run ufw default allow outgoing
run ufw allow 22/tcp
run ufw allow 80/tcp
run ufw allow 443/tcp
run ufw --force enable

cat <<'NEXT'

Provisioned. Next, from your dev machine:  deploy/deploy.sh <ip>

To enable the agent later (M6), create the key file on this VM. It's absent on purpose:

  install -m 600 -o faultforge -g faultforge /dev/null /etc/faultforge/inference.env
  nano /etc/faultforge/inference.env

It holds these two lines, and goes nowhere else. Don't paste the key on a command line:

  VULTR_INFERENCE_API_KEY=...
  FF_AGENT_ENABLED=1

Then: systemctl restart faultforge
NEXT
