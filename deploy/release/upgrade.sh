#!/usr/bin/env bash
# Upgrade this installation to a newer bundle. Run from the CURRENT install:
#   ./upgrade.sh /path/to/rag-onprem-NEWVERSION
# Steps: verify the new bundle, back up, load the new images, update compose
# files and scripts (keeping .env and certs), start. Database migrations run
# automatically when the new api starts.
# Rollback: the old images stay loaded. Set RAG_VERSION back in .env, run
# `docker compose up -d`, then ./restore.sh the backup this script made.
source "$(dirname "$0")/lib.sh"
load_env

new="${1:-}"
[ -n "$new" ] && [ -f "$new/VERSION" ] || die "usage: $0 /path/to/new/bundle"
new="$(cd "$new" && pwd)"
[ "$new" != "$BUNDLE_DIR" ] || die "Run this from the current installation, pointing at the NEW bundle"
new_version="$(sed -n 's/^RAG_VERSION=//p' "$new/.env.example")"
info "Upgrading $RAG_VERSION -> $new_version"

info "Verifying the new bundle"
verify_checksums "$new"
ok "intact"

info "Backing up first"
bash ./backup.sh
backup="$(ls -1d backups/rag-onprem-* | tail -1)"

info "Loading new images"
gzip -dc "$new/images.tar.gz" | docker load | sed 's/^/  /'

info "Updating compose files and scripts (keeping .env and certs)"
for f in docker-compose.yml docker-compose.gpu.yml lib.sh preflight.sh install.sh \
    create-admin.sh backup.sh restore.sh upgrade.sh README.md VERSION .env.example; do
    cp "$new/$f" "./$f.new" && mv "./$f.new" "./$f"
done
rm -rf nginx && cp -r "$new/nginx" ./nginx
cp "$new/SHA256SUMS" ./SHA256SUMS
cp "$new/images.tar.gz" ./images.tar.gz

# New settings get their defaults; existing ones are left alone.
while IFS= read -r line; do
    key="${line%%=*}"
    case "$line" in ''|'#'*) continue ;; esac
    grep -qE "^${key}=" .env || { echo "$line" >>.env; ok "added new setting ${key}"; }
done <.env.example
set_env RAG_VERSION "$new_version"
# The models are baked into the images, so they follow the bundle.
for key in LLM_MODEL EMBED_MODEL EMBED_DIM RERANKER_MODEL; do
    set_env "$key" "$(sed -n "s/^${key}=//p" .env.example)"
done

info "Starting $new_version"
dc up -d --remove-orphans
if ! wait_healthy 900; then
    warn "Not healthy. To roll back:"
    warn "  set RAG_VERSION=$RAG_VERSION in .env, run: docker compose up -d && ./restore.sh $backup"
    exit 1
fi
ok "upgraded to $new_version (backup: $backup)"
