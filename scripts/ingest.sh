#!/usr/bin/env bash
# Ingest one or more PDFs into the running stack.
#   bash scripts/ingest.sh samples/sample.pdf [more.pdf ...]
source "$(dirname "$0")/_common.sh"
require_env

if [ "$#" -eq 0 ]; then
    echo "usage: $0 file.pdf [more.pdf ...]" >&2
    exit 2
fi

status=0
for file in "$@"; do
    if [ ! -f "$file" ]; then
        echo "No such file: $file" >&2
        status=1
        continue
    fi
    name="$(basename "$file")"
    compose cp "$(native_path "$file")" "api:/tmp/${name}"
    compose exec -T api python -m app.ingestion.cli "/tmp/${name}" || status=1
    compose exec -T -u root api rm -f "/tmp/${name}"
done
exit "$status"
