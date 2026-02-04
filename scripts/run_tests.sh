#!/usr/bin/env bash
# SPDX-FileCopyrightText: 2026 AI Power Grid
#
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# Run lint and (optionally) integration tests. Matches CI flow.
# Requires: venv with pip install -r requirements.txt -r requirements.dev.txt
#           Postgres + Redis (e.g. docker compose up -d postgres redis)
# Usage: from repo root, with venv activated:
#   ./scripts/run_tests.sh          # lint only
#   ./scripts/run_tests.sh --full  # lint + start server + pytest

set -e
cd "$(dirname "$0")/.."

echo "=== Lint (black + ruff) ==="
black --check .
ruff check .

if [[ "${1:-}" != "--full" ]]; then
  echo "Lint passed. Use ./scripts/run_tests.sh --full to run integration tests (needs server + Postgres + Redis)."
  exit 0
fi

echo "=== Starting server (background) ==="
export POSTGRES_URL="${POSTGRES_URL:-localhost:5432/postgres}"
export PGUSER="${PGUSER:-postgres}"
export PGPASSWORD="${PGPASSWORD:-postgres}"
export POSTGRES_USER="${POSTGRES_USER:-postgres}"
export POSTGRES_PASS="${POSTGRES_PASS:-$PGPASSWORD}"
export REDIS_IP="${REDIS_IP:-localhost}"
export REDIS_SERVERS="${REDIS_SERVERS:-[\"localhost\"]}"
export USE_SQLITE="${USE_SQLITE:-0}"
export ADMINS="${ADMINS:-[\"test_user#1\"]}"
export KUDOS_TRUST_THRESHOLD="${KUDOS_TRUST_THRESHOLD:-100}"

python server.py -vvvvi --horde stable &
SERVER_PID=$!
trap "kill $SERVER_PID 2>/dev/null || true" EXIT

echo "Waiting for server..."
for i in $(seq 1 120); do
  if curl -s http://localhost:7001/ > /dev/null 2>&1; then
    echo "Server ready after ${i}s"
    break
  fi
  if [[ $i -eq 120 ]]; then
    echo "Server failed to start within 120s"
    exit 1
  fi
  sleep 1
done

echo "=== Register test user ==="
curl -s -X POST --data-raw 'username=test_user' http://localhost:7001/register | grep -Po '<code class="api-key-display">\K[^<]+' > tests/apikey.txt || true
if [[ ! -s tests/apikey.txt ]]; then
  echo "Could not get API key from /register"
  exit 1
fi

echo "=== pytest ==="
pytest tests/ -s
