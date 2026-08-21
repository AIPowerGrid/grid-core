#!/usr/bin/env bash
# SPDX-FileCopyrightText: 2026 AI Power Grid
# SPDX-License-Identifier: AGPL-3.0-or-later

# Create one consistent, root-only PostgreSQL custom-format backup.
# POSTGRES_* is supplied by /etc/aipg/grid.env through systemd.

set -euo pipefail
umask 077

BACKUP_DIR="${AIPG_BACKUP_DIR:-/var/lib/aipg-backup}"
RETENTION_DAYS="${AIPG_BACKUP_RETENTION_DAYS:-14}"
BACKUP_SCHEMA="${AIPG_BACKUP_SCHEMA:-public}"

die() {
    echo "error: $*" >&2
    exit 1
}

for name in POSTGRES_USER POSTGRES_PASS POSTGRES_URL; do
    [[ -n "${!name:-}" ]] || die "$name is required"
done
[[ "$RETENTION_DAYS" =~ ^[0-9]+$ ]] || die "AIPG_BACKUP_RETENTION_DAYS must be a non-negative integer"
[[ "$BACKUP_SCHEMA" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]] ||
    die "AIPG_BACKUP_SCHEMA must be one PostgreSQL identifier"
[[ "$POSTGRES_URL" =~ ^([A-Za-z0-9._-]+):([0-9]{1,5})/([A-Za-z0-9_-]+)$ ]] ||
    die "POSTGRES_URL must use host:port/database form"

host="${BASH_REMATCH[1]}"
port="${BASH_REMATCH[2]}"
database="${BASH_REMATCH[3]}"
(( port >= 1 && port <= 65535 )) || die "POSTGRES_URL port is invalid"
[[ "$POSTGRES_USER" != *$'\n'* && "$POSTGRES_PASS" != *$'\n'* ]] ||
    die "PostgreSQL credentials cannot contain newlines"

for tool in flock mktemp pg_dump pg_restore sha256sum; do
    command -v "$tool" >/dev/null 2>&1 || die "$tool is required"
done

install -d -m 0700 "$BACKUP_DIR"
exec 9>"$BACKUP_DIR/.backup.lock"
flock -n 9 || die "another Grid PostgreSQL backup is running"

passfile="$(mktemp "$BACKUP_DIR/.pgpass.XXXXXX")"
partial=""
cleanup() {
    rm -f -- "$passfile"
    if [[ -n "$partial" ]]; then
        rm -f -- "$partial"
    fi
}
trap cleanup EXIT

escape_pgpass() {
    printf '%s' "$1" | sed -e 's/\\/\\\\/g' -e 's/:/\\:/g'
}

printf '%s:%s:%s:%s:%s\n' \
    "$(escape_pgpass "$host")" \
    "$port" \
    "$(escape_pgpass "$database")" \
    "$(escape_pgpass "$POSTGRES_USER")" \
    "$(escape_pgpass "$POSTGRES_PASS")" >"$passfile"
chmod 0600 "$passfile"
export PGPASSFILE="$passfile"

stamp="$(date -u +%Y%m%dT%H%M%SZ)"
filename="grid-postgres-${stamp}.dump"
final="$BACKUP_DIR/$filename"
partial="$BACKUP_DIR/.${filename}.partial"
[[ ! -e "$final" && ! -e "$final.sha256" ]] ||
    die "refusing to replace an existing backup for $stamp"

pg_dump \
    --host="$host" \
    --port="$port" \
    --username="$POSTGRES_USER" \
    --dbname="$database" \
    --schema="$BACKUP_SCHEMA" \
    --format=custom \
    --compress=9 \
    --file="$partial"

pg_restore --list "$partial" >/dev/null
chmod 0600 "$partial"
mv -- "$partial" "$final"
partial=""
(cd "$BACKUP_DIR" && sha256sum -- "$filename" >"$filename.sha256")
chmod 0600 "$final.sha256"

find "$BACKUP_DIR" -maxdepth 1 -type f \
    \( -name 'grid-postgres-*.dump' -o -name 'grid-postgres-*.dump.sha256' \) \
    -mtime "+$RETENTION_DAYS" -delete

echo "Grid PostgreSQL backup verified: $final"
echo "Checksum manifest: $final.sha256"
