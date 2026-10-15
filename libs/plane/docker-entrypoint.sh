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
  reactor)
    # Only for a hub run with HANGAR_REACTOR_ENABLED=false; by default the hub runs the reactor.
    exec python -m app.reactor "$@"
    ;;
  migrate)
    exec alembic "$@"
    ;;
  *)
    exec "$mode" "$@"
    ;;
esac
