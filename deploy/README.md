# Fresh production deployment (Proxmox)

Replaces the legacy single-box deployment (pkill/nohup, hardcoded salt,
shared-everything). Fresh VM, fresh database, generated secrets, systemd,
journald logs, nginx in front.

**Why fresh DB:** every API key in the old database was hashed with the
publicly-known hardcoded salt (`s0m3s3cr3t` — the .env secret never got
parsed). The new code refuses to boot with that salt. Users re-register;
the userbase is small enough that a clean break beats a migration.

## 1. Provision the VM (Proxmox)

- Ubuntu Server 24.04, 8+ vCPU, 16+ GB RAM, 100+ GB disk
- Public IP (or NAT + port-forward 80/443)

## 2. Bootstrap

```bash
git clone https://github.com/AIPowerGrid/system-core.git
cd system-core && sudo bash deploy/bootstrap.sh
```

The script installs everything, creates the fresh DB, **generates GRID_SALT
and the DB password**, and starts 8 Flask procs + grid_api under systemd.

## 3. The two manual secrets steps

1. **Dashboard salt sync** — copy the generated salt into grid-frontend's
   Vercel env (`GRID_SALT`) and redeploy the dashboard. All three systems
   (Flask, grid_api, dashboard) must share it.
   ```bash
   grep GRID_SALT /etc/aipg/grid.env
   ```
2. **TLS** — once DNS for `api.aipowergrid.io` points at the new box:
   ```bash
   certbot --nginx -d api.aipowergrid.io
   ```

## 4. Cutover

1. Verify locally on the box first:
   ```bash
   curl -s http://127.0.0.1:7010/v1/models            # grid_api up
   curl -s http://127.0.0.1:7001/api/v2/status/models  # flask up
   ```
2. Lower the DNS TTL ahead of time; flip `api.aipowergrid.io` A record
   (Cloudflare) to the new IP.
3. Old box: stop accepting new work, let in-flight jobs drain, then shut
   the services down. Keep the old DB dump archived (it contains no
   plaintext keys, but history may be useful).
4. Announce re-registration: keys from the old system no longer work
   (Discord + a banner on the register page).

## 5. Day-2 operations

| Task | Command |
|---|---|
| Logs (live) | `journalctl -u aipg-gridapi -f` / `journalctl -u aipg-horde@7001 -f` |
| Restart API | `systemctl restart aipg-gridapi` |
| Rolling Flask restart | `for p in {7001..7008}; do systemctl restart aipg-horde@$p; sleep 5; done` |
| Deploy new code | `sudo -u aipg git -C /home/aipg/system-core pull && rolling restart` |
| Status | `systemctl status 'aipg-*'` |

Post-deploy verification checks: see `DEPLOY_grid_api.md` in the repo root.

## Notes

- grid_api runs on **7010**; Flask owns 7001-7008 (images.py proxies to 7001).
- `grid_den_events` and Flask tables are created automatically on first boot
  against the empty database.
- Rate limiting + quota use Redis DB 7 and fail open if Redis is down.
- Workers and the registration page need no changes — same public URLs.
