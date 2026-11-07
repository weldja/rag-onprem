#!/bin/sh
# Apply database migrations, then run the given command (uvicorn by default).
set -eu
alembic upgrade head
exec "$@"
