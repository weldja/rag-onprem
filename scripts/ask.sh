#!/usr/bin/env bash
# Ask a question of the running stack as a given user.
#   RAG_USER=alice RAG_PASSWORD=... bash scripts/ask.sh "How many days of leave can I carry over?"
source "$(dirname "$0")/_common.sh"

if [ "$#" -eq 0 ] || [ -z "${RAG_USER:-}" ] || [ -z "${RAG_PASSWORD:-}" ]; then
    echo "usage: RAG_USER=... RAG_PASSWORD=... $0 \"question\"" >&2
    exit 2
fi

json_string() {  # minimal JSON string escaping for backslashes and double quotes
    printf '"%s"' "$(printf '%s' "$1" | sed -e 's/\\/\\\\/g' -e 's/"/\\"/g')"
}

jar="$(mktemp)"
trap 'rm -f "$jar"' EXIT
csrf=(-H "X-Requested-With: rag-onprem" -H "Content-Type: application/json")

"${CURL[@]}" -f -c "$jar" "${csrf[@]}" -o /dev/null "${BASE_URL}/api/auth/login" \
    --data "{\"username\": $(json_string "$RAG_USER"), \"password\": $(json_string "$RAG_PASSWORD")}" \
    || { echo "Sign-in failed" >&2; exit 1; }

"${CURL[@]}" --max-time 900 -b "$jar" "${csrf[@]}" "${BASE_URL}/api/ask" \
    --data "{\"question\": $(json_string "$*")}"
echo
