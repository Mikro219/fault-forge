# Deploying FaultForge

One Ubuntu 24.04 VM, with FaultForge as the only thing on it.

**Membership of the `docker` group is root-equivalent.** The app's user can start a privileged container, so it can effectively do anything on the VM. That's why the app is alone on the VM and takes no free-form input: a request can only choose one of two fixed targets. The hardening in `faultforge.service` limits what a bug can do directly, but it isn't a boundary against someone who controls the app.

Sandboxes are containers that share the VM's kernel. gVisor is an optional later step, and isn't installed by these scripts.

## Steps

Create the VM yourself, then with its address in `IP`:

```bash
scp deploy/provision.sh root@$IP:/root/provision.sh
ssh root@$IP bash /root/provision.sh
deploy/deploy.sh $IP
```

The site is then at `https://<ip-with-dashes>.sslip.io/`, for example `https://203-0-113-7.sslip.io/`. To update it, run `deploy/deploy.sh $IP` again.

If you also use a Vultr firewall group, allow only TCP 22, 80 and 443 there too.

## Enabling the agent (M6)

It's off until you do this. Create the file on the VM, using an editor so the key isn't on a command line or in shell history:

```bash
install -m 600 -o faultforge -g faultforge /dev/null /etc/faultforge/inference.env
nano /etc/faultforge/inference.env
```

```
VULTR_INFERENCE_API_KEY=...
FF_AGENT_ENABLED=1
```

Then run `systemctl restart faultforge`. Confirm the model's price in the Vultr console against `AGENT_PRICES` in `src/faultforge/web/settings.py` first. The caps are $2 a day and $15 in total, set with `FF_DAILY_USD_CAP` and `FF_TOTAL_USD_CAP`. Spending is recorded in `/var/lib/faultforge/data/budget.json`.

## Checklist on the VM

These were checked on the first deployment on 2026-11-06. Repeat them on any new VM.

- [ ] `docker info --format '{{.SecurityOptions}}'` lists `apparmor`, as well as `seccomp` and `cgroupns`. This couldn't be checked under WSL2.
- [ ] A run's container uses the profile: during a run, `docker ps -q --filter label=faultforge.sandbox | xargs docker inspect --format '{{.AppArmorProfile}}'` prints `docker-default`.
- [ ] The sandbox tests pass on the VM, as the app's user. Docker's setup differs from the one in `docs/VERIFIED.md`, particularly the bridge gateway in the egress test. This builds a separate environment in `/tmp` and doesn't touch the app's.

  ```bash
  cd /opt/faultforge
  sudo -u faultforge -H env UV_PROJECT_ENVIRONMENT=/tmp/ff-test-venv UV_CACHE_DIR=/tmp/ff-uv-cache PYTHONDONTWRITEBYTECODE=1 \
    /usr/local/bin/uv run --frozen --extra web --python /usr/bin/python3 pytest -o addopts="" -q -p no:cacheprovider tests/test_sandbox.py tests/test_web.py
  ```

- [ ] The app runs as expected: `systemctl status faultforge` is active, and `ps -o user= -C python` shows `faultforge`. It's listening on loopback only: `ss -ltnp | grep 8080` shows `127.0.0.1:8080`.
- [ ] Firewall: `ufw status verbose` shows deny incoming, with only 22, 80 and 443 allowed. From another machine, `nc -vz -w 3 $IP 8080` fails.
- [ ] HTTPS: `curl -sSI https://<hostname>/` shows a valid certificate and the `Content-Security-Policy` header, and `curl -sS https://<hostname>/healthz` returns `{"status":"ok"}`. On certificate trouble, read `journalctl -u caddy`. sslip.io is a shared domain, so Let's Encrypt's rate limits can get in the way, and Caddy falls back to ZeroSSL.
- [ ] A forged header doesn't dodge the cooldown: two quick runs with different `X-Forwarded-For` values from one machine give a 429 on the second, with reason `cooldown`.
- [ ] A real run reaches `FAIL` from the public URL, and `hostile-build` reaches `INCONCLUSIVE`, with `containers_left` 0 in `/api/status` afterwards.
- [ ] No secrets on disk or in logs: `/etc/faultforge/inference.env` is mode 600 and owned by `faultforge`, if it exists, and `journalctl -u faultforge | grep -c VULTR_INFERENCE_API_KEY` prints 0.

## Where things are

| Path | What |
|---|---|
| `/opt/faultforge` | The code, owned by root and read-only to the app |
| `/var/lib/faultforge/data/projects` | Run artifacts. Removed after 24 hours, or beyond the newest 20 (`FF_RETENTION_AGE_S`, `FF_RETENTION_KEEP`) |
| `/var/lib/faultforge/data/replays` | Stored real runs. Never removed automatically. The first 5 of each kind are kept |
| `/var/lib/faultforge/data/budget.json` | Spending. Deleting it resets the caps |
| `/etc/faultforge/inference.env` | The key, if the agent is enabled |

Logs: `journalctl -u faultforge` and `journalctl -u caddy`.
