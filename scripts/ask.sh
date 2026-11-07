#!/usr/bin/env bash
# Ask a question of the running stack.
#   bash scripts/ask.sh "How many days of leave can I carry over?"
source "$(dirname "$0")/_common.sh"

if [ "$#" -eq 0 ]; then
    echo "usage: $0 \"question\"" >&2
    exit 2
fi

question="$*"
# Minimal JSON string escaping for backslashes and double quotes.
escaped="$(printf '%s' "$question" | sed -e 's/\\/\\\\/g' -e 's/"/\\"/g')"
curl -sS --max-time 900 -X POST "${BASE_URL}/api/ask" \
    -H "Content-Type: application/json" \
    --data "{\"question\": \"${escaped}\"}"
echo
