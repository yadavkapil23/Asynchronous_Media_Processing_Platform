#!/usr/bin/env bash
set -euo pipefail

backup_dir="${BACKUP_DIR:-./backups}"
stamp="$(date -u +%Y%m%dT%H%M%SZ)"
mkdir -p "$backup_dir"
database_file="$backup_dir/forge-${stamp}.sql.gz"
media_file="$backup_dir/media-${stamp}.tar.gz"
backup_mount="$(cd "$backup_dir" && pwd)"

docker compose exec -T postgres pg_dump -U "${POSTGRES_USER:-forge}" -d "${POSTGRES_DB:-forge}" --format=plain | gzip -9 > "$database_file"
MSYS_NO_PATHCONV=1 docker run --rm -v "$(docker volume ls -q | grep '_media-data$' | head -n 1):/source:ro" -v "$backup_mount:/backup" alpine:3.20 tar -czf "/backup/$(basename "$media_file")" -C /source .
printf 'Created %s and %s\n' "$database_file" "$media_file"
