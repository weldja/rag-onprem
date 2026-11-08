#!/usr/bin/env bash
# Create an administrator account. The password is read at a hidden prompt
# (or from stdin when not interactive) and never appears in process lists.
#   ./create-admin.sh USERNAME
source "$(dirname "$0")/lib.sh"
[ "$#" -eq 1 ] || die "usage: $0 USERNAME"
load_env

if [ -t 0 ]; then
    # IFS= keeps leading/trailing spaces: they're part of the password.
    IFS= read -rsp "Password for $1: " password; echo
    IFS= read -rsp "Repeat password: " repeat; echo
    [ "$password" = "$repeat" ] || die "Passwords don't match"
else
    IFS= read -r password
fi
printf '%s\n' "$password" | dc exec -T api python -m app.admin create-user "$1" --admin --password-stdin
