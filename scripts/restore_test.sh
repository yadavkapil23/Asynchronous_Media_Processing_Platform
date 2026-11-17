#!/usr/bin/env bash
set -euo pipefail

backup_dir="${BACKUP_DIR:-./backups}"
database_file="${1:?Usage: BACKUP_DIR=./backups ./scripts/restore_test.sh backup.sql.gz media.tar.gz}"
media_file="${2:?Usage: BACKUP_DIR=./backups ./scripts/restore_test.sh backup.sql.gz media.tar.gz}"
test_database="forge_restore_test"
backup_mount="$(cd "$backup_dir" && pwd)"
media_volume="${MEDIA_VOLUME:-$(docker volume ls -q | grep '_media-data$' | head -n 1)}"

cleanup() {
    docker compose exec -T postgres psql -U "${POSTGRES_USER:-forge}" -d postgres -c "DROP DATABASE IF EXISTS ${test_database};" >/dev/null
}
trap cleanup EXIT

docker compose exec -T postgres psql -U "${POSTGRES_USER:-forge}" -d postgres -c "CREATE DATABASE ${test_database};" >/dev/null
gzip -dc "$database_file" | docker compose exec -T postgres psql -U "${POSTGRES_USER:-forge}" -d "$test_database" >/dev/null
MSYS_NO_PATHCONV=1 docker run --rm -v "$media_volume:/target" -v "$backup_mount:/backup:ro" alpine:3.20 sh -c "rm -rf /target/* && tar -xzf /backup/$(basename "$media_file") -C /target"
docker compose exec -T postgres psql -U "${POSTGRES_USER:-forge}" -d "$test_database" -c "SELECT version_num FROM alembic_version;" >/dev/null
printf 'Restore verification passed for %s and %s\n' "$database_file" "$media_file"
