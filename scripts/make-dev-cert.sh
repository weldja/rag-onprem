#!/usr/bin/env bash
# Create a self-signed certificate for development in deploy/certs/.
# Customers install their own certificate as deploy/certs/tls.crt + tls.key.
#   bash scripts/make-dev-cert.sh [--force]
source "$(dirname "$0")/_common.sh"

certs="$ROOT/deploy/certs"
if [ -f "$certs/tls.crt" ] && [ "${1:-}" != "--force" ]; then
    echo "deploy/certs/tls.crt already exists (use --force to replace it)"
    exit 0
fi
mkdir -p "$certs"
# The nginx image has no openssl CLI; use a throwaway Alpine container.
docker run --rm -v "$(native_path "$certs"):/certs" alpine:3.20 sh -c '
    apk add --no-cache openssl >/dev/null &&
    openssl req -x509 -newkey rsa:2048 -nodes -days 825 \
        -subj "/CN=localhost" \
        -addext "subjectAltName=DNS:localhost,IP:127.0.0.1,DNS:proxy" \
        -keyout /certs/tls.key -out /certs/tls.crt 2>/dev/null &&
    chmod 644 /certs/tls.crt && chmod 600 /certs/tls.key'
echo "Created self-signed deploy/certs/tls.crt (development only)"
