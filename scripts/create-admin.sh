#!/usr/bin/env bash
# Create the first administrator (run once after installing).
#   bash scripts/create-admin.sh USERNAME
# The password is typed at a hidden prompt and passed on stdin, never as an
# argument, so it doesn't appear in process lists or shell history.
source "$(dirname "$0")/_common.sh"
require_env

if [ "$#" -ne 1 ]; then
    echo "usage: $0 USERNAME" >&2
    exit 2
fi

# IFS= keeps leading/trailing spaces: they're part of the password.
IFS= read -rsp "Password for $1: " password; echo
IFS= read -rsp "Repeat password: " repeat; echo
if [ "$password" != "$repeat" ]; then
    echo "Passwords don't match" >&2
    exit 1
fi
printf '%s\n' "$password" | compose exec -T api python -m app.admin create-user "$1" --admin --password-stdin
