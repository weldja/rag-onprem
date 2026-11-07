#!/usr/bin/env bash
# Verify that the backend containers cannot reach the internet.
# Passes only if every outbound attempt FAILS.
source "$(dirname "$0")/_common.sh"
require_env

failed=0
for svc in api worker ollama db; do
    if compose exec -T "$svc" timeout 5 bash -c 'exec 3<>/dev/tcp/1.1.1.1/443' 2>/dev/null; then
        echo "FAIL $svc: reached 1.1.1.1:443 directly"
        failed=1
    else
        echo "ok   $svc: no direct route to the internet"
    fi
    if compose exec -T "$svc" timeout 5 getent hosts example.com >/dev/null 2>&1; then
        echo "FAIL $svc: resolved example.com"
        failed=1
    else
        echo "ok   $svc: external DNS does not resolve"
    fi
done
exit "$failed"
