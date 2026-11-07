#!/usr/bin/env bash
# Directory sign-in against a real OpenLDAP server with TLS (test-only image).
# Starts the server on the internal backend network, seeds users and groups,
# runs tests/ldap_live, then removes the server. Needs the dev stack running.
source "$(dirname "$0")/_common.sh"
require_env

name=rag-onprem-test-ldap
image="osixia/openldap:1.5.0@sha256:18742e9c449c9c1afe129d3f2f3ee15fb34cc43e5f940a20f3399728f41d7c28"
docker rm -f "$name" >/dev/null 2>&1 || true  # leftover from an interrupted run
ca_dir="$(mktemp -d)"
# mktemp makes the directory 0700; slapd runs as another uid inside its
# container and must be able to read the certificates mounted from here.
chmod 755 "$ca_dir"
cleanup() { docker rm -f "$name" >/dev/null 2>&1 || true; rm -rf "$ca_dir"; }
trap cleanup EXIT

# Fresh short-lived CA and server certificate for ldap.test on every run. (The
# image's bundled CA has expired, and our client correctly refuses it.)
docker run --rm -v "$(native_path "$ca_dir"):/certs" \
    alpine:3.20@sha256:d9e853e87e55526f6b2917df91a2115c36dd7c696a35be12163d44e6e2a4b6bc sh -c '
    set -e
    apk add --no-cache openssl >/dev/null
    cd /certs
    openssl req -x509 -newkey rsa:2048 -nodes -days 2 -subj "/CN=rag-onprem test CA" \
        -keyout ca.key -out ca.crt 2>/dev/null
    openssl req -newkey rsa:2048 -nodes -subj "/CN=ldap.test" \
        -addext "subjectAltName=DNS:ldap.test" -keyout ldap.key -out ldap.csr 2>/dev/null
    openssl x509 -req -in ldap.csr -CA ca.crt -CAkey ca.key -CAcreateserial -days 2 \
        -copy_extensions copy -out ldap.crt 2>/dev/null
    # The image'"'"'s slapd uses an older GnuTLS: give it a traditional RSA key
    # and standard DH parameters.
    openssl rsa -in ldap.key -traditional -out ldap.key 2>/dev/null
    openssl dhparam -out dhparam.pem 1024 2>/dev/null
    chmod 644 ./*'

docker run -d --name "$name" --hostname ldap.test --network rag-onprem_backend --network-alias ldap.test \
    -v "$(native_path "$ca_dir"):/container/service/slapd/assets/certs" \
    -e LDAP_ORGANISATION=Example -e LDAP_DOMAIN=example.test \
    -e LDAP_ADMIN_PASSWORD=test-admin-password -e LDAP_TLS_VERIFY_CLIENT=never \
    -e LDAP_TLS_CRT_FILENAME=ldap.crt -e LDAP_TLS_KEY_FILENAME=ldap.key \
    -e LDAP_TLS_CA_CRT_FILENAME=ca.crt -e LDAP_TLS_DH_PARAM_FILENAME=dhparam.pem \
    "$image" --copy-service >/dev/null

echo "Waiting for slapd..."
up=0
for _ in $(seq 1 120); do
    # Bind as admin: the image's ACLs hide the base entry from anonymous
    # searches (reported as "no such object") once start-up has finished.
    if docker exec "$name" ldapsearch -x -H ldap://localhost -D cn=admin,dc=example,dc=test \
        -w test-admin-password -b dc=example,dc=test -s base >/dev/null 2>&1; then
        up=1
        break
    fi
    sleep 1
done
if [ "$up" != 1 ]; then
    echo "OpenLDAP did not start (start-up messages, without connection noise):" >&2
    docker logs "$name" 2>&1 | grep -v ' conn=' | tail -40 >&2 || true
    exit 1
fi

docker exec -i "$name" ldapadd -x -H ldap://localhost -D cn=admin,dc=example,dc=test \
    -w test-admin-password <"$ROOT/backend/tests/ldap_live/seed.ldif" >/dev/null

docker build -q --target dev -t rag-onprem/api:test "$ROOT_NATIVE/backend" >/dev/null
docker run --rm --network rag-onprem_backend \
    -v "$(native_path "$ca_dir"):/ldap-ca:ro" \
    rag-onprem/api:test pytest -m ldap tests/ldap_live -v "$@"
