#!/usr/bin/env bash
set -euo pipefail
echo 'WARNING: legacy/incomplete backup; no writer drainage or protected configuration. Community instances must use scripts/backup_community.py.' >&2

ENV_FILE="${1:-deploy/service/env.production}"
COMPOSE_FILE="${COMPOSE_FILE:-deploy/service/compose.yml}"
BACKUP_DIR="${BACKUP_DIR:-state/backups}"

if [[ ! -f "$ENV_FILE" ]]; then
  echo "Missing $ENV_FILE" >&2
  exit 1
fi

env_dir="$(cd "$(dirname "$ENV_FILE")" && pwd)"
env_file_abs="$env_dir/$(basename "$ENV_FILE")"
compose_dir="$(cd "$(dirname "$COMPOSE_FILE")" && pwd)"
compose_file_abs="$compose_dir/$(basename "$COMPOSE_FILE")"
mkdir -p "$BACKUP_DIR"
backup_dir_abs="$(cd "$BACKUP_DIR" && pwd)"

set -a
# shellcheck disable=SC1090
source "$env_file_abs"
set +a

# Refuse a full media archive that could exhaust the shared application disk.
# Uncompressed size is conservative because media is generally already compressed.
reserve_path="${RESERVE_HOST_PATH:-}"
if [[ -n "$reserve_path" && "$reserve_path" != /* ]]; then
  reserve_path="$(realpath -m "$compose_dir/$reserve_path")"
fi
if [[ "${BACKUP_RESERVE:-1}" == "1" && -d "$reserve_path" ]]; then
  reserve_bytes="$(du -sb "$reserve_path" | cut -f1)"
  available_bytes="$(df -B1 --output=avail "$backup_dir_abs" | tail -1 | tr -d ' ')"
  if (( available_bytes < reserve_bytes + 1073741824 )); then
    echo "Insufficient backup capacity: reserve archive requires its source size plus 1 GiB headroom; use a larger backup destination or BACKUP_RESERVE=0 for database-only backup." >&2
    exit 1
  fi
fi

stamp="$(date -u +%Y%m%dT%H%M%SZ)"
out="$backup_dir_abs/meme_arsenal_${stamp}.dump"
reserve_out="$backup_dir_abs/meme_arsenal_reserve_${stamp}.tar.gz"
dump_tmp="${out}.tmp"
reserve_tmp="${reserve_out}.tmp"

cleanup() {
  rm -f "$dump_tmp" "$reserve_tmp"
}
trap cleanup EXIT

SERVICE_ENV_FILE="$env_file_abs" docker compose -f "$compose_file_abs" --env-file "$env_file_abs" \
  exec -T db pg_dump -U "${POSTGRES_USER:-meme_arsenal}" -d "${POSTGRES_DB:-meme_arsenal}" -Fc > "$dump_tmp"
SERVICE_ENV_FILE="$env_file_abs" docker compose -f "$compose_file_abs" --env-file "$env_file_abs" \
  exec -T db pg_restore --list < "$dump_tmp" > /dev/null
mv "$dump_tmp" "$out"
chmod 600 "$out"
sha256sum "$out" > "${out}.sha256"
chmod 600 "${out}.sha256"
echo "Wrote $out"

reserve_path="${RESERVE_HOST_PATH:-}"
if [[ -n "$reserve_path" && "$reserve_path" != /* ]]; then
  reserve_path="$(realpath -m "$compose_dir/$reserve_path")"
fi

if [[ "${BACKUP_RESERVE:-1}" == "1" && -n "$reserve_path" && -d "$reserve_path" ]]; then
  tar -C "$reserve_path" -czf "$reserve_tmp" .
  tar -tzf "$reserve_tmp" > /dev/null
  mv "$reserve_tmp" "$reserve_out"
  chmod 600 "$reserve_out"
  sha256sum "$reserve_out" > "${reserve_out}.sha256"
  chmod 600 "${reserve_out}.sha256"
  echo "Wrote $reserve_out"
else
  echo "Skipping reserve backup; BACKUP_RESERVE is disabled or RESERVE_HOST_PATH is not a directory." >&2
fi

if [[ "${BACKUP_RETENTION_DAYS:-}" =~ ^[0-9]+$ ]]; then
  find "$backup_dir_abs" -maxdepth 1 -type f \
    \( -name 'meme_arsenal_*.dump' -o -name 'meme_arsenal_*.dump.sha256' \
       -o -name 'meme_arsenal_reserve_*.tar.gz' -o -name 'meme_arsenal_reserve_*.tar.gz.sha256' \) \
    -mtime "+${BACKUP_RETENTION_DAYS}" -delete
fi
