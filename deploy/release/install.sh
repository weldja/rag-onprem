#!/usr/bin/env bash
# Install (or re-start) rag-onprem from this bundle. Needs no internet access.
#   bash install.sh            check, load images, configure, start
#   FORCE=1 bash install.sh    continue even if preflight finds a problem
source "$(dirname "$0")/lib.sh"

info "rag-onprem $(cat VERSION 2>/dev/null | head -1)"

info "Preflight"
if ! preflight_out="$(bash ./preflight.sh)"; then
    printf '%s\n' "$preflight_out"
    [ "${FORCE:-0}" = 1 ] || die "Preflight failed. Fix the problems above, or re-run with FORCE=1."
    warn "continuing because FORCE=1"
else
    printf '%s\n' "$preflight_out"
fi
gpu="$(printf '%s\n' "$preflight_out" | tail -1 | sed -n 's/^GPU=//p')"

info "Verifying bundle checksums"
verify_checksums .
ok "all files intact"

info "Loading images (several minutes)"
gzip -dc images.tar.gz | docker load | sed 's/^/  /'

info "Configuration"
if [ -f .env ]; then
    ok ".env exists: keeping it"
    set_env RAG_VERSION "$(sed -n 's/^RAG_VERSION=//p' .env.example)"
else
    cp .env.example .env
    set_env POSTGRES_PASSWORD "$(random_secret 32)"
    chmod 600 .env
    ok "created .env with a random database password"
fi
if [ "$gpu" = 1 ]; then
    set_env COMPOSE_FILE "docker-compose.yml:docker-compose.gpu.yml"
    ok "GPU enabled"
fi

if [ -f certs/tls.crt ] && [ -f certs/tls.key ]; then
    ok "TLS certificate found in certs/"
elif command -v openssl >/dev/null 2>&1; then
    host="$(hostname -f 2>/dev/null || hostname)"
    openssl req -x509 -newkey rsa:2048 -nodes -days 825 -subj "/CN=${host}" \
        -addext "subjectAltName=DNS:${host},DNS:localhost,IP:127.0.0.1" \
        -keyout certs/tls.key -out certs/tls.crt 2>/dev/null
    chmod 600 certs/tls.key
    warn "no certificate supplied: created a SELF-SIGNED one for ${host}."
    warn "Browsers will warn until you replace certs/tls.crt and certs/tls.key (see README.md)."
else
    die "No certs/tls.crt + certs/tls.key and no openssl to create one. Add the certificate and re-run."
fi

info "Starting (the first start loads the model into memory)"
dc up -d
if ! wait_healthy 900; then
    dc ps
    die "The stack did not become healthy within 15 minutes. See: docker compose logs api ollama"
fi
ok "healthy"

load_env
echo
info "Installed. Open https://$(hostname -f 2>/dev/null || hostname):${HTTPS_PORT:-443}/"
if [ -z "$(dc exec -T api python -m app.admin list-users 2>/dev/null)" ]; then
    echo "  Create the first administrator:  bash create-admin.sh USERNAME"
fi
echo "  Schedule backups:                 bash backup.sh   (see README.md)"
