#!/usr/bin/env bash
# Restore a backup made by backup.sh, REPLACING all current data.
#   ./restore.sh backups/rag-onprem-YYYYMMDD-HHMMSS [--yes]
# Restore into the same rag-onprem version the backup was made with, then
# upgrade if needed. Configuration (.env, certs) is not overwritten; copy it
# from the backup's config/ yourself when moving to a new server.
source "$(dirname "$0")/lib.sh"
load_env

src="${1:-}"
[ -n "$src" ] && [ -d "$src" ] || die "usage: $0 BACKUP_DIRECTORY [--yes]"
[ -f "$src/database.dump" ] && [ -f "$src/uploads.tar.gz" ] || die "$src is not a backup made by backup.sh"
verify_checksums "$src"

backup_version="$(sed -n 's/^rag_version=//p' "$src/MANIFEST")"
if [ "$backup_version" != "$RAG_VERSION" ]; then
    die "Backup is from version $backup_version but this install runs $RAG_VERSION. Restore with the matching bundle first, then upgrade."
fi

echo "This replaces ALL documents, users and settings with the backup from:"
sed 's/^/  /' "$src/MANIFEST"
if [ "${2:-}" != "--yes" ]; then
    read -rp "Type 'restore' to continue: " answer
    [ "$answer" = restore ] || die "Cancelled"
fi

info "Stopping the application (the database keeps running)"
dc stop proxy worker api

info "Restoring the database"
dc exec -T db dropdb -U rag --if-exists rag
dc exec -T db createdb -U rag rag
dc exec -T db pg_restore -U rag -d rag --no-owner --exit-on-error <"$src/database.dump"
ok "database"

info "Restoring uploaded files"
dc run --rm --no-deps -T --user root --entrypoint sh \
    -v "$(native_path "$src"):/backup:ro" \
    worker -c 'find /data/uploads -mindepth 1 -delete && tar -C /data/uploads -xzf /backup/uploads.tar.gz && chown -R 10001 /data/uploads'
ok "uploaded files"

info "Starting"
dc up -d
wait_healthy 900 || die "Not healthy after restore. See: docker compose logs api"
ok "restored $(sed -n 's/^contents=//p' "$src/MANIFEST")"
