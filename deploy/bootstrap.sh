#!/usr/bin/env bash
# AIPG fresh-production bootstrap (Ubuntu 24.04 on Proxmox).
# Idempotent-ish: safe to re-run; skips what already exists.
#
# Run as root:  bash deploy/bootstrap.sh
#
# What it does:
#   1. System packages (python3.11+, postgres, redis, nginx, certbot)
#   2. aipg user + repo checkout + venv
#   3. Postgres: fresh DB + user with generated password, max_connections=300
#   4. /etc/aipg/grid.env from template with GENERATED secrets (incl GRID_SALT)
#   5. systemd units (8x Flask + grid_api) + nginx site
#
# What it does NOT do (manual, see deploy/README.md):
#   - DNS cutover, certbot run, dashboard GRID_SALT sync, worker onboarding

set -euo pipefail

REPO_URL="${REPO_URL:-https://github.com/AIPowerGrid/system-core.git}"
BRANCH="${BRANCH:-feature/p2p-libp2p}"
APP_DIR=/home/aipg/system-core
ENV_FILE=/etc/aipg/grid.env

echo "── [1/5] packages ──"
apt-get update -qq
apt-get install -y -qq git python3 python3-venv python3-dev build-essential \
    postgresql postgresql-contrib redis-server nginx certbot python3-certbot-nginx \
    libpq-dev openssl

echo "── [2/5] aipg user + checkout ──"
id aipg &>/dev/null || useradd -m -s /bin/bash aipg
if [ ! -d "$APP_DIR/.git" ]; then
    sudo -u aipg git clone --branch "$BRANCH" "$REPO_URL" "$APP_DIR"
else
    sudo -u aipg git -C "$APP_DIR" fetch origin && sudo -u aipg git -C "$APP_DIR" checkout "$BRANCH" && sudo -u aipg git -C "$APP_DIR" pull
fi
if [ ! -d "$APP_DIR/.venv" ]; then
    sudo -u aipg python3 -m venv "$APP_DIR/.venv"
fi
sudo -u aipg "$APP_DIR/.venv/bin/pip" install --quiet --upgrade pip
sudo -u aipg "$APP_DIR/.venv/bin/pip" install --quiet -r "$APP_DIR/requirements.txt" uvicorn

echo "── [3/5] postgres (fresh DB) ──"
DB_PASS=$(openssl rand -hex 24)
sudo -u postgres psql -tc "SELECT 1 FROM pg_roles WHERE rolname='aipg'" | grep -q 1 || \
    sudo -u postgres psql -c "CREATE ROLE aipg LOGIN PASSWORD '$DB_PASS';"
sudo -u postgres psql -tc "SELECT 1 FROM pg_database WHERE datname='aipg_grid'" | grep -q 1 || \
    sudo -u postgres psql -c "CREATE DATABASE aipg_grid OWNER aipg;"

# pg_cron — REQUIRED. The horde sql_statements schedule stored procedures via
# pg_cron and directly UPDATE cron.job. Without it, every Flask proc crashes on
# boot with "schema cron does not exist". Detect the installed PG major version.
PGVER=$(ls /etc/postgresql/ | sort -V | tail -1)
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq "postgresql-${PGVER}-cron"
PG_CONFD="/etc/postgresql/${PGVER}/main/conf.d"
mkdir -p "$PG_CONFD"
cat > "$PG_CONFD/pg_cron.conf" <<EOF
shared_preload_libraries = 'pg_cron'
cron.database_name = 'aipg_grid'
EOF

# Connection budget: 8 Flask procs * (10+15) + grid_api pools + headroom
PGCONF=$(sudo -u postgres psql -tc "SHOW config_file" | xargs)
grep -q "^max_connections = 300" "$PGCONF" || \
    sed -i 's/^max_connections.*/max_connections = 300/' "$PGCONF"
systemctl restart postgresql
sleep 4

# pg_cron extension + grant the app user the privileges the sql_statements need
# (they UPDATE cron.job directly, which a non-owner can't do without grants).
sudo -u postgres psql -d aipg_grid -c "CREATE EXTENSION IF NOT EXISTS pg_cron;"
sudo -u postgres psql -d aipg_grid -c "GRANT USAGE ON SCHEMA cron TO aipg; GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA cron TO aipg; GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA cron TO aipg;"

echo "── [4/5] /etc/aipg/grid.env (generated secrets) ──"
mkdir -p /etc/aipg
if [ ! -f "$ENV_FILE" ]; then
    GRID_SALT=$(openssl rand -hex 32)
    sed -e "s|^GRID_SALT=.*|GRID_SALT=$GRID_SALT|" \
        -e "s|^POSTGRES_PASS=.*|POSTGRES_PASS=$DB_PASS|" \
        "$APP_DIR/deploy/env.template" > "$ENV_FILE"
    chmod 600 "$ENV_FILE" && chown aipg:aipg "$ENV_FILE"
    echo ""
    echo "  ┌─────────────────────────────────────────────────────────────┐"
    echo "  │  GRID_SALT generated. COPY IT to the dashboard's GRID_SALT   │"
    echo "  │  env (grid-frontend on Vercel) or dashboard-issued keys      │"
    echo "  │  will not validate against this API.                         │"
    echo "  │  View: grep GRID_SALT $ENV_FILE                              │"
    echo "  └─────────────────────────────────────────────────────────────┘"
else
    echo "  $ENV_FILE exists — leaving secrets untouched."
fi

echo "── [5/5] systemd + nginx ──"
cp "$APP_DIR/deploy/systemd/aipg-horde@.service" /etc/systemd/system/
cp "$APP_DIR/deploy/systemd/aipg-gridapi.service" /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now redis-server
systemctl enable --now aipg-horde@{7001..7008}
systemctl enable --now aipg-gridapi
cp "$APP_DIR/deploy/nginx/aipg-api.conf" /etc/nginx/sites-available/
ln -sf /etc/nginx/sites-available/aipg-api.conf /etc/nginx/sites-enabled/
nginx -t && systemctl reload nginx

echo ""
echo "✅ Bootstrap complete. Next (see deploy/README.md):"
echo "   1. certbot --nginx -d api.aipowergrid.io   (after DNS points here)"
echo "   2. Sync GRID_SALT to the dashboard env on Vercel"
echo "   3. Verify: curl -s http://127.0.0.1:7010/v1/models"
echo "   4. journalctl -u aipg-gridapi -f   # watch logs"
