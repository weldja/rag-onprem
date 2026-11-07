#!/usr/bin/env bash
# Back up the database, uploaded files and configuration while running.
#   ./backup.sh [DIRECTORY]     default: ./backups
# Creates DIRECTORY/rag-onprem-<UTC timestamp>/ containing:
#   database.dump    pg_dump custom format (users, documents, chunks, vectors)
#   uploads.tar.gz   original uploaded files
#   config/          .env and certs/ (SECRETS: database password, TLS key)
#   MANIFEST         version, time, row counts
# The backup holds sensitive data: store it as securely as the server itself.
source "$(dirname "$0")/lib.sh"
load_env

root="${1:-backups}"
stamp="$(date -u +%Y%m%d-%H%M%S)"
out="$root/rag-onprem-$stamp"
mkdir -p "$out/config"
chmod 700 "$out"
info "Backing up to $out"

# Database first, then files: a file without a row is harmless after restore,
# a row without its file is not.
dc exec -T db pg_dump -U rag -d rag -Fc >"$out/database.dump"
ok "database ($(du -h "$out/database.dump" | cut -f1))"

dc run --rm --no-deps -T --user root --entrypoint tar \
    -v "$(native_path "$out"):/backup" \
    worker -C /data/uploads -czf /backup/uploads.tar.gz .
ok "uploaded files ($(du -h "$out/uploads.tar.gz" | cut -f1))"

cp .env "$out/config/.env"
cp -r certs "$out/config/certs"
chmod -R go-rwx "$out/config"
ok "configuration"

counts="$(dc exec -T db psql -U rag -d rag -At -c \
    "SELECT (SELECT count(*) FROM documents) || ' documents, ' || (SELECT count(*) FROM users) || ' users'")"
{
    echo "rag_version=${RAG_VERSION}"
    echo "created_utc=${stamp}"
    echo "contents=${counts}"
} >"$out/MANIFEST"
(cd "$out" && sha256sum database.dump uploads.tar.gz >SHA256SUMS)
ok "$counts"
info "Done: $out"
