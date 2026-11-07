#!/usr/bin/env bash
# Stop the dev stack. Pass -v to also delete the database volume.
source "$(dirname "$0")/_common.sh"
require_env
compose down "$@"
