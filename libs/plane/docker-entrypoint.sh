#!/bin/sh
set -eu

mode="${1:-api}"
[ $# -gt 0 ] && shift || true

case "$mode" in
  api)
    exec python -m app.api "$@"
    ;;
  hub)
    exec python -m app.hub "$@"
    ;;
  migrate)
    exec alembic "$@"
    ;;
  *)
    exec "$mode" "$@"
    ;;
esac
