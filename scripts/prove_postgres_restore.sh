#!/usr/bin/env bash
# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

# Restore one Grid backup into a disposable local database, migrate it with the
# selected immutable release, and prove Alembic/schema parity. Must run as root.

set -euo pipefail
umask 077

die() {
    echo "error: $*" >&2
    exit 1
}

BACKUP="${AIPG_RESTORE_BACKUP:-}"
CANDIDATE="${AIPG_RESTORE_CANDIDATE:-/home/aipg/current}"
KEEP_SCRATCH="${AIPG_RESTORE_KEEP_SCRATCH:-0}"
RUN_AS_CURRENT_USER="${AIPG_RESTORE_RUN_AS_CURRENT_USER:-0}"
MIGRATION_PYTHON="${AIPG_RESTORE_PYTHON:-$CANDIDATE/.venv/bin/python}"
ADMIN_USER="${AIPG_RESTORE_ADMIN_USER:-}"
ADMIN_PASS="${AIPG_RESTORE_ADMIN_PASS:-}"

[[ "$RUN_AS_CURRENT_USER" == 0 || "$RUN_AS_CURRENT_USER" == 1 ]] ||
    die "AIPG_RESTORE_RUN_AS_CURRENT_USER must be 0 or 1"
if [[ "$RUN_AS_CURRENT_USER" -eq 0 ]]; then
    [[ "${EUID:-$(id -u)}" -eq 0 ]] || die "run as root"
fi

for name in POSTGRES_USER POSTGRES_PASS POSTGRES_URL; do
    [[ -n "${!name:-}" ]] || die "$name is required"
done
[[ "$POSTGRES_USER" != *$'\n'* && "$POSTGRES_PASS" != *$'\n'* ]] ||
    die "PostgreSQL credentials cannot contain newlines"
[[ -n "$BACKUP" && -f "$BACKUP" ]] || die "AIPG_RESTORE_BACKUP must name an existing dump"
[[ -f "$BACKUP.sha256" ]] || die "backup checksum manifest is missing"
[[ -x "$MIGRATION_PYTHON" && -f "$CANDIDATE/alembic.ini" ]] ||
    die "AIPG_RESTORE_CANDIDATE is not a prepared Grid release"
[[ "$KEEP_SCRATCH" == 0 || "$KEEP_SCRATCH" == 1 ]] ||
    die "AIPG_RESTORE_KEEP_SCRATCH must be 0 or 1"
[[ "$POSTGRES_URL" =~ ^(127\.0\.0\.1|localhost):([0-9]{1,5})/([A-Za-z0-9_-]+)$ ]] ||
    die "restore proof only supports a local PostgreSQL host"

host="${BASH_REMATCH[1]}"
port="${BASH_REMATCH[2]}"
source_database="${BASH_REMATCH[3]}"
(( port >= 1 && port <= 65535 )) || die "POSTGRES_URL port is invalid"

for tool in createdb dropdb mktemp pg_restore psql sha256sum; do
    command -v "$tool" >/dev/null 2>&1 || die "$tool is required"
done
if [[ -n "$ADMIN_USER" ]]; then
    [[ -n "$ADMIN_PASS" ]] || die "AIPG_RESTORE_ADMIN_PASS is required with its admin user"
    [[ "$ADMIN_USER" != *$'\n'* && "$ADMIN_PASS" != *$'\n'* ]] ||
        die "restore admin credentials cannot contain newlines"
elif [[ "$RUN_AS_CURRENT_USER" -eq 1 ]]; then
    die "current-user mode requires explicit restore admin credentials"
else
    command -v runuser >/dev/null 2>&1 || die "runuser is required"
    id postgres >/dev/null 2>&1 || die "the local postgres service account is required"
fi
if [[ "$RUN_AS_CURRENT_USER" -eq 0 ]]; then
    id aipg >/dev/null 2>&1 || die "the aipg service account is required"
fi

manifest_target="$(awk 'NR == 1 {print $2}' "$BACKUP.sha256")"
[[ "$manifest_target" == "$(basename "$BACKUP")" && "$(wc -l <"$BACKUP.sha256")" -eq 1 ]] ||
    die "checksum manifest is not bound to the selected backup"
(cd "$(dirname "$BACKUP")" && sha256sum --check --strict "$(basename "$BACKUP.sha256")")
pg_restore --list "$BACKUP" >/dev/null

scratch="aipg_restore_proof_$(date -u +%Y%m%d_%H%M%S)_$$"
[[ "$scratch" =~ ^aipg_restore_proof_[A-Za-z0-9_]+$ ]] || die "unsafe scratch database name"
scratch_created=0
passfile="$(mktemp /var/tmp/aipg-restore-pgpass.XXXXXX)"
admin_passfile=""

cleanup() {
    local original_status=$?
    local drop_status=0
    trap - EXIT
    set +e
    if [[ "$scratch_created" -eq 1 && "$KEEP_SCRATCH" -eq 0 ]]; then
        if [[ -n "$ADMIN_USER" ]]; then
            PGPASSFILE="$admin_passfile" dropdb \
                --host="$host" \
                --port="$port" \
                --username="$ADMIN_USER" \
                --maintenance-db=postgres \
                --if-exists \
                --force \
                "$scratch" >/dev/null
            drop_status=$?
        else
            runuser -u postgres -- dropdb \
                --port="$port" \
                --if-exists \
                --force \
                "$scratch" >/dev/null
            drop_status=$?
        fi
    fi
    rm -f -- "$passfile"
    if [[ -n "$admin_passfile" ]]; then
        rm -f -- "$admin_passfile"
    fi
    if [[ "$original_status" -eq 0 && "$drop_status" -ne 0 ]]; then
        exit "$drop_status"
    fi
    exit "$original_status"
}
trap cleanup EXIT

escape_pgpass() {
    printf '%s' "$1" | sed -e 's/\\/\\\\/g' -e 's/:/\\:/g'
}

printf '%s:%s:%s:%s:%s\n' \
    "$(escape_pgpass "$host")" \
    "$port" \
    "$(escape_pgpass "$scratch")" \
    "$(escape_pgpass "$POSTGRES_USER")" \
    "$(escape_pgpass "$POSTGRES_PASS")" >"$passfile"
chmod 0600 "$passfile"
export PGPASSFILE="$passfile"

if [[ -n "$ADMIN_USER" ]]; then
    admin_passfile="$(mktemp /var/tmp/aipg-restore-admin-pgpass.XXXXXX)"
    printf '%s:%s:*:%s:%s\n' \
        "$(escape_pgpass "$host")" \
        "$port" \
        "$(escape_pgpass "$ADMIN_USER")" \
        "$(escape_pgpass "$ADMIN_PASS")" >"$admin_passfile"
    chmod 0600 "$admin_passfile"
    PGPASSFILE="$admin_passfile" createdb \
        --host="$host" \
        --port="$port" \
        --username="$ADMIN_USER" \
        --maintenance-db=postgres \
        --owner="$POSTGRES_USER" \
        "$scratch"
else
    runuser -u postgres -- createdb --port="$port" --owner="$POSTGRES_USER" "$scratch"
fi
scratch_created=1
psql \
    --host="$host" \
    --port="$port" \
    --username="$POSTGRES_USER" \
    --dbname="$scratch" \
    --set=ON_ERROR_STOP=1 \
    --command='DROP SCHEMA public'
pg_restore \
    --exit-on-error \
    --no-owner \
    --no-privileges \
    --host="$host" \
    --port="$port" \
    --username="$POSTGRES_USER" \
    --dbname="$scratch" \
    "$BACKUP"

export POSTGRES_URL="$host:$port/$scratch"
if [[ "$RUN_AS_CURRENT_USER" -eq 0 ]]; then
    export HOME=/home/aipg
fi
unset GRID_DB_URL
cd "$CANDIDATE"
run_migration() {
    if [[ "$RUN_AS_CURRENT_USER" -eq 1 ]]; then
        "$MIGRATION_PYTHON" -m alembic "$@"
    else
        runuser -u aipg --preserve-environment -- "$MIGRATION_PYTHON" -m alembic "$@"
    fi
}
run_migration upgrade head
run_migration current
run_migration check

current="$(
    psql \
        --host="$host" \
        --port="$port" \
        --username="$POSTGRES_USER" \
        --dbname="$scratch" \
        --tuples-only \
        --no-align \
        --command='SELECT version_num FROM alembic_version'
)"
head="$(run_migration heads | awk 'NR == 1 {print $1}')"
[[ "$current" == "$head" ]] || die "restored database revision $current does not match head $head"

echo "Restore proof passed: $source_database backup -> $scratch -> Alembic $current"
if [[ "$KEEP_SCRATCH" -eq 1 ]]; then
    echo "Scratch database retained by request: $scratch"
fi
